"""Black-box acceptance of the shipped executables, outside the checkout."""
from __future__ import annotations
import argparse
import configparser
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from desktop_pet.settings_manager import SettingsManager
from desktop_pet.perception.settings import PerceptionSettings
from tests.mock_api import MockApiServer

OUT=ROOT/'artifacts/release-validation/native'
USER=ctypes.WinDLL('user32',use_last_error=True)
USER.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
USER.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
USER.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
USER.IsWindowVisible.argtypes=[wintypes.HWND]
USER.IsIconic.argtypes=[wintypes.HWND]
USER.GetWindow.argtypes=[wintypes.HWND,wintypes.UINT]
USER.GetWindow.restype=wintypes.HWND
USER.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
PWSH=shutil.which('pwsh')
assert PWSH, 'The test driver requires PowerShell 7; the delivered application does not.'

def windows(pid):
    rows=[]
    @ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def each(hwnd,_):
        owner=wintypes.DWORD()
        USER.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value!=pid:return True
        title,kind=ctypes.create_unicode_buffer(512),ctypes.create_unicode_buffer(128)
        USER.GetWindowTextW(hwnd,title,512);USER.GetClassNameW(hwnd,kind,128)
        if kind.value.startswith('Qt'):
            rectangle=wintypes.RECT();USER.GetWindowRect(hwnd,ctypes.byref(rectangle))
            rows.append({'hwnd':int(hwnd),'title':title.value,
                'visible':bool(USER.IsWindowVisible(hwnd)),'minimized':bool(USER.IsIconic(hwnd)),
                'owner':int(USER.GetWindow(hwnd,4) or 0),
                'area':max(0,rectangle.right-rectangle.left)*max(0,rectangle.bottom-rectangle.top)})
        return True
    USER.EnumWindows(each,0)
    return rows

def wait(predicate,seconds=30,description='condition'):
    limit=time.monotonic()+seconds
    while time.monotonic()<limit:
        result=predicate()
        if result:return result
        time.sleep(.15)
    raise AssertionError('Acceptance timed out: '+description)

def find_window(process,title,visible=True):
    def get():
        if process.poll() is not None:raise AssertionError('Delivered executable exited early: '+str(process.returncode))
        found=[row for row in windows(process.pid) if row['title']==title and (not visible or row['visible'])]
        return found[0] if len(found)==1 else None
    return wait(get,description=title)

def uia(process,window,action='inspect',name='',value='',control_type='',keys=''):
    for attempt in range(3):
        matches=[row for row in windows(process.pid) if row['title']==window['title']]
        if len(matches)==1:window=matches[0]
        command=[PWSH,'-NoProfile','-File',str(Path(__file__).parent/'native_uia.ps1'),'-Handle',str(window['hwnd']),
            '-ExpectedPid',str(process.pid),'-Action',action]
        for label,text in (('-Name',name),('-Value',value),('-Type',control_type),('-Keys',keys)):
            if text:command.extend((label,text))
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=25)
        if result.returncode==0:break
        if 'Window is not owned by the acceptance process.' not in result.stderr:
            raise AssertionError('Owned UI action failed: '+result.stderr[-2000:])
        time.sleep(.4)
    if result.returncode:raise AssertionError('Owned UI handle did not settle: '+result.stderr[-2000:])
    return json.loads(result.stdout.lstrip('\ufeff')) if result.stdout.strip() else None

def named(process,window,name,kind=None):
    rows=uia(process,window)
    return [row for row in rows if row['name']==name and (kind is None or row['type']==kind)]

def main():
    global OUT
    parser=argparse.ArgumentParser();parser.add_argument('payload',type=Path)
    parser.add_argument('--report-directory',type=Path,default=OUT)
    parser.add_argument('--test-copy',type=Path);args=parser.parse_args()
    OUT=args.report_directory.resolve();OUT.mkdir(parents=True,exist_ok=False)
    payload=args.payload.resolve()
    assert (payload/'鲸伴.exe').is_file()
    if args.test_copy:
        copied=args.test_copy.resolve();work=copied.parent
        expected=json.loads((payload/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
        actual=json.loads((copied/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
        assert actual==expected
        assert (copied/'鲸伴.exe').read_bytes()==(payload/'鲸伴.exe').read_bytes()
    else:
        work=Path(tempfile.mkdtemp(dir='E:/',prefix='鲸伴 独立封装验收 '));copied=work/'软件 中文 空格'
    profile=work/('全新测试配置 '+str(time.time_ns()))
    profile.mkdir()
    report={'source_payload':str(payload),'test_copy':str(copied),'profile':str(profile),
        'different_machine':False,'same_host_fresh_profile':True,'physical_mouse_used':False,
        'production_vault_read':False,'checks':{},'success':False}
    assert not work.is_relative_to(ROOT)
    assert shutil.disk_usage(work).free>sum(p.stat().st_size for p in payload.rglob('*') if p.is_file())+512*1024*1024
    if not args.test_copy:shutil.copytree(payload,copied)
    env=os.environ.copy()
    for key in tuple(env):
        if any(word in key.upper() for word in ('API_KEY','API_TOKEN','AUTH_TOKEN','SECRET','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')):
            env.pop(key,None)
    env['PATH']=str(Path(env.get('SystemRoot',r'C:\Windows'))/'System32')+os.pathsep+env.get('SystemRoot',r'C:\Windows')
    env['NO_PROXY']='127.0.0.1,localhost'
    exe=copied/'鲸伴.exe'
    stdout=(OUT/'candidate_stdout.log').open('wb');stderr=(OUT/'candidate_stderr.log').open('wb')
    process=None
    settings=SettingsManager(profile_dir=profile)
    settings.save_perception_settings(PerceptionSettings(enabled=False, allow_remote=False))
    report['qa_screen_observation_explicitly_disabled'] = True
    assert settings.get_api_key()==''
    assert settings.load_theme()=='light' and settings.load_scale()==100
    fresh_agent=settings.load_agent_settings()
    assert fresh_agent.enabled and fresh_agent.tools_enabled and fresh_agent.computer_enabled
    report['checks']['fresh_light_scale100_all_agent_permissions']=True
    fake='local-only-packaged-release-acceptance'
    settings.save_api_key(fake)
    try:
        with MockApiServer() as server:
            server.enqueue('{"reply":"独立封装包可以正常聊天。","emotion":"happy"}')
            process=subprocess.Popen([str(exe),'--profile-dir',str(profile),'--qa-exit-after-ms','600000'],
                cwd=str(work),env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            pet=find_window(process,'鲸伴')
            report['checks']['standalone_pet_launch']=True
            activation=subprocess.run([str(exe),'--profile-dir',str(profile)],cwd=str(work),env=env,
                stdin=subprocess.DEVNULL,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert activation.returncode==0
            chat=find_window(process,'和鲸鱼娘聊天 - 鲸伴')
            wait(lambda:named(process,chat,'发送给鲸伴的消息','ControlType.Edit'),description='WebEngine composer')
            assert len([r for r in windows(process.pid) if r['title']=='鲸伴'])==1
            assert named(process,chat,'鲸伴','ControlType.Document')
            report['checks']['single_instance_and_webengine']=True

            uia(process,chat,'invoke','设置',control_type='ControlType.Button')
            setting_window=find_window(process,'设置 · 鲸伴')
            uia(process,setting_window,'keys','设置分类',control_type='ControlType.List',keys='40,40')
            wait(lambda:named(process,setting_window,'全局 API 根地址（Base URL）','ControlType.Edit'),description='API page shown')
            uia(process,setting_window,'set','全局 API 根地址（Base URL）',value=server.base_url,control_type='ControlType.Edit')
            uia(process,setting_window,'set','聊天模型名称',value='local-release-model',control_type='ControlType.Edit')
            uia(process,setting_window,'invoke','保存配置',control_type='ControlType.Button')
            def saved_api():
                configuration=configparser.ConfigParser(interpolation=None)
                configuration.read(profile/'settings.ini',encoding='utf-8-sig')
                return (configuration.get('api','base_url',fallback='')==server.base_url
                        and configuration.get('api','model',fallback='')=='local-release-model')
            wait(saved_api,description='settings write completes after asynchronous InvokePattern')
            report['checks']['settings_saved_in_fresh_profile']=True
            uia(process,setting_window,'invoke','返回聊天',control_type='ControlType.Button')
            chat=find_window(process,'和鲸鱼娘聊天 - 鲸伴')
            question='请确认这个独立测试包可以正常聊天。'
            uia(process,chat,'type','发送给鲸伴的消息',value=question,control_type='ControlType.Edit')
            wait(lambda:any(row['enabled'] for row in named(process,chat,'发送消息','ControlType.Button')),description='message draft accepted')
            uia(process,chat,'invoke','发送消息',control_type='ControlType.Button')
            def has_reply():
                db=profile/'conversations.sqlite3'
                if not db.exists():return False
                with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as connection:
                    return connection.execute('SELECT count(*) FROM messages WHERE content=?',('独立封装包可以正常聊天。',)).fetchone()[0]==1
            wait(has_reply,seconds=45,description='actual SDK localhost reply')
            assert server.requests and server.requests[0]['authorization']=='Bearer '+fake
            report['question_expected']=question
            report['question_received']=server.requests[0]['body']['messages'][-1]['content']
            assert server.requests[0]['body']['messages'][-1]['content']==question
            report['checks']['actual_localhost_sdk_chat']=True

            def settled_reply():
                return (not named(process,chat,'停止生成','ControlType.Button')
                    and uia(process,chat,'value','发送给鲸伴的消息',control_type='ControlType.Edit')['value']=='')
            wait(settled_reply,description='reply and composer clear have rendered')

            draft='这是一段关闭重开后仍应保留的独立测试草稿。'
            uia(process,chat,'type','发送给鲸伴的消息',value=draft,control_type='ControlType.Edit')
            report['draft_expected']=draft
            wait(lambda:uia(process,chat,'value','发送给鲸伴的消息',control_type='ControlType.Edit')['value']==draft,
                 description='full draft rendered before hiding')
            report['draft_before_hide']=uia(process,chat,'value','发送给鲸伴的消息',control_type='ControlType.Edit')['value']
            uia(process,chat,'close')
            wait(lambda:not any(r['title']=='和鲸鱼娘聊天 - 鲸伴' and r['visible'] for r in windows(process.pid)),description='chat hides')
            reopened=subprocess.run([str(exe),'--profile-dir',str(profile)],cwd=str(work),env=env,stdin=subprocess.DEVNULL,
                capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert reopened.returncode==0
            chat=find_window(process,'和鲸鱼娘聊天 - 鲸伴')
            def restored_draft():
                value=uia(process,chat,'value','发送给鲸伴的消息',control_type='ControlType.Edit')['value']
                report['draft_after_reopen']=value
                return value==draft
            wait(restored_draft,description='reopened composer draft hydration')
            uia(process,chat,'set','发送给鲸伴的消息',value='',control_type='ControlType.Edit')
            report['checks']['hide_reopen_and_draft_preservation']=True

            for name,title,board,keys in (
                ('gomoku','和鲸鱼娘下五子棋 - 鲸伴','五子棋棋盘','13'),
                ('xiangqi','和鲸鱼娘下象棋 - 鲸伴','象棋棋盘','38,38,38,13,38,13'),
            ):
                entry='和鲸鱼娘下五子棋' if name=='gomoku' else '和鲸鱼娘下象棋'
                uia(process,chat,'invoke','和鲸鱼娘玩耍',control_type='ControlType.Button')
                wait(lambda:named(process,chat,entry,'ControlType.Button'),description='expanded games menu')
                uia(process,chat,'invoke',entry,control_type='ControlType.Button')
                game=find_window(process,title)
                hint='请求五子棋提示' if name=='gomoku' else '请求走棋提示'
                wait(lambda:any(row['enabled'] for row in named(process,game,hint)),description=name+' local engine ready')
                uia(process,game,'keys',board,control_type='ControlType.Group',keys=keys)
                wait(lambda:named(process,game,'棋谱 · 2','ControlType.TabItem'),seconds=30,description=name+' native human/AI turn')
                report['checks'][name+'_entry_engine_native_turn']=True
                uia(process,game,'close')

            selected={'version':1,'action':'ask_selection','context':{'text':'公开测试的合成选区','title':'独立测试网页',
                'url':'https://example.invalid/testing?token=not-real#fixture','page_text':'仅用于本地封装验收。',
                'captured_at':datetime.now(timezone.utc).isoformat(),'app_name':'Acceptance fixture'}}
            frame=json.dumps(selected,ensure_ascii=False).encode('utf-8')
            bridge=subprocess.run([str(copied/'鲸伴网页桥.exe'),'--config-path',str(profile/'settings.ini'),
                'chrome-extension://gbeikpndpckhjeakbckijelidpdpinei/'],
                input=struct.pack('<I',len(frame))+frame,capture_output=True,env=env,cwd=str(work),timeout=30,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert len(bridge.stdout)>=4
            length=struct.unpack('<I',bridge.stdout[:4])[0]
            assert len(bridge.stdout)==length+4
            response=json.loads(bridge.stdout[4:].decode('utf-8'))
            assert bridge.returncode==0 and response.get('ok') is True,response
            report['checks']['frozen_native_bridge_exact_json_frame']=True
            assert not (copied/'agent_workspace').exists()
            report['checks']['no_workspace_at_application_root']=True
            report['test_profile_configuration_written']=True
            report['success']=True
    except BaseException as error:
        report['error']={'type':type(error).__name__,'message':str(error)[-4000:]}
        if process is not None:
            report['owned_windows_at_failure']=windows(process.pid)
            report['owned_accessibility_at_failure']={}
            for window in windows(process.pid):
                if window['visible']:
                    try:report['owned_accessibility_at_failure'][window['title']]=uia(process,window)
                    except Exception:pass
        raise
    finally:
        if process is not None and process.poll() is None:
            pets=[row for row in windows(process.pid) if row['title']=='鲸伴']
            candidates=[row for row in pets if row['owner']==0] or pets
            if candidates:
                uia(process,max(candidates,key=lambda row:row['area']),'close')
                report['close_message_sent']=True
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:report['normal_shutdown_timed_out']=True
        report['main_process_exit_code']=process.returncode if process is not None else None
        settings.clear_api_key()
        assert settings.get_api_key()==''
        report['isolated_fixture_key_removed']=True
        stdout.close();stderr.close()
        (OUT/'candidate_native_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True),flush=True)

if __name__=='__main__':main()
