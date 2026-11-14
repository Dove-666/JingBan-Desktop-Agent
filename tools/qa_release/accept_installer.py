"""Install/uninstall only this owned test copy; leave real user shortcuts/data alone."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import winreg

OUT=Path(__file__).resolve().parents[2]/'artifacts/release-validation/installer'
APP_ID='{754D3BD6-BAF9-468F-AB55-D88BC479CE1B}_is1'
REGISTRY=r'Software\Microsoft\Windows\CurrentVersion\Uninstall'+'\\'+APP_ID

def sha(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):result.update(block)
    return result.hexdigest()

def registration():
    found=[]
    for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,REGISTRY,0,winreg.KEY_READ|view) as key:
                found.append({'view':view,'name':winreg.QueryValueEx(key,'DisplayName')[0]})
        except FileNotFoundError:pass
    return found

def startup_digest():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run',0,winreg.KEY_READ) as key:
            value=winreg.QueryValueEx(key,'鲸伴')
    except FileNotFoundError:value=None
    return hashlib.sha256(json.dumps(value,ensure_ascii=False).encode('utf-8')).hexdigest()

def parse_arguments(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('installer',type=Path);parser.add_argument('payload',type=Path)
    parser.add_argument('--report-directory',type=Path,default=OUT)
    parser.add_argument('--non-interactive',action='store_true',
        help='Install/check/uninstall without desktop interaction; the installed application runs --help only.')
    parser.add_argument('--verify-dsh-runtime',action='store_true',
        help='Before uninstall, verify the installed DSH runtime without native input.')
    parser.add_argument('--verify-private-literals',action='store_true',
        help='Before uninstall, compare installed bytes with locally held private values; no values are logged.')
    return parser.parse_args(argv)

def interactive_driver():
    # Non-interactive acceptance must not even load the UIA/window driver.
    spec=importlib.util.spec_from_file_location('candidate_qa',Path(__file__).parent/'accept_candidate.py')
    qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)
    return qa

def wait(predicate,seconds=30,description='condition'):
    limit=time.monotonic()+seconds
    while time.monotonic()<limit:
        result=predicate()
        if result:return result
        time.sleep(.15)
    raise AssertionError('Acceptance timed out: '+description)

def profile_digests(profile):
    return {p.relative_to(profile).as_posix():sha(p) for p in profile.rglob('*') if p.is_file()}

def help_probe(exe,work,profile,environment,output):
    # app.main parses --help before _run: no QApplication, profile, single
    # instance activation, pet, chat, perception or native input is created.
    before=profile_digests(profile)
    result=subprocess.run([str(exe),'--help'],cwd=str(work),env=environment,
        stdin=subprocess.DEVNULL,capture_output=True,timeout=30,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (output/'installed_help_stdout.log').write_bytes(result.stdout)
    (output/'installed_help_stderr.log').write_bytes(result.stderr)
    assert result.returncode==0,('Installed executable --help failed',result.returncode)
    assert profile_digests(profile)==before,'The CLI help probe changed the owned profile'
    return result.returncode

def optional_background_checks(args,installation,work,output,report):
    root=Path(__file__).resolve().parents[2]
    checks=[]
    if args.verify_dsh_runtime:
        checks.append(('installed_dsh_runtime',[
            str(installation/'_internal/runtime/node/node.exe'),str(root/'tools/qa_release/dsh_candidate.mjs'),
            '--payload',str(installation),'--skip-native-input','--output',str(output/'installed_dsh_runtime.json')],300))
    if args.verify_private_literals:
        checks.append(('installed_private_literals',[
            sys.executable,str(root/'tools/qa_release/verify_private_literals.py'),
            '--payload',str(installation),'--report',str(output/'installed_private_literals.json')],600))
    for name,command,timeout in checks:
        # Keep the real audit environment. The privacy verifier reads only its
        # existing getters and must not substitute the synthetic test profile.
        result=subprocess.run(command,cwd=str(work),env=os.environ.copy(),stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        report['checks'][name]={'exit_code':result.returncode,'report':str(output/(name+'.json'))}
        assert result.returncode==0,(name+' failed',result.returncode)

def main(argv=None):
    global OUT
    args=parse_arguments(argv);installer=args.installer.resolve();payload=args.payload.resolve()
    qa=None if args.non_interactive else interactive_driver()
    OUT=args.report_directory.resolve();OUT.mkdir(parents=True,exist_ok=False)
    assert installer.is_file() and (payload/'PACKAGE-MANIFEST.json').is_file()
    build=json.loads((installer.parent/'build.json').read_text(encoding='utf-8'))
    assert build['inputs']['manifest']==sha(payload/'PACKAGE-MANIFEST.json'),'Installer input manifest differs from frozen candidate'
    assert build['sha256']==sha(installer),'Installer bytes differ from finished build report'
    before=registration();assert not before,'This AppId is already installed; no test installation performed.'
    boundary=Path(r'E:\鲸伴 发行独立验收').resolve();boundary.mkdir(exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix='安装与卸载 ',dir=boundary)).resolve()
    installation=work/'程序 中文 空格';profile=work/'独立用户资料';profile.mkdir()
    assert work.is_relative_to(boundary) and not installation.exists()
    report={'installer':str(installer),'installer_sha256':sha(installer),'source_payload':str(payload),
        'installation':str(installation),'profile':str(profile),'preexisting_app_registration':before,
        'no_icons':True,'same_host_fresh_profile':True,'success':False,'checks':{},
        'non_interactive':args.non_interactive,
        'scope':'install-bytes-cli-uninstall-preservation' if args.non_interactive else 'install-native-ui-bridge-uninstall-preservation',
        'interactive_ui_test':'not_run_desktop_busy' if args.non_interactive else 'pending',
        'startup_probe':'installed_exe_help_only' if args.non_interactive else 'native_webengine'}
    shortcut=Path.home()/'Desktop/鲸伴.lnk'
    shortcut_before=sha(shortcut) if shortcut.is_file() else None
    startup_before=startup_digest()
    marker=profile/'必须保留的测试资料.txt';marker.write_text('This test user data must survive uninstall.',encoding='utf-8')
    if args.non_interactive:
        # Seed only owned synthetic preservation fixtures. The installed
        # application is never asked to open or migrate this profile.
        (profile/'settings.ini').write_text('[qa]\nsynthetic_preservation_fixture=true\n',encoding='utf-8')
        with sqlite3.connect(profile/'conversations.sqlite3') as database:
            database.execute('CREATE TABLE qa_preservation (content TEXT NOT NULL)')
            database.execute('INSERT INTO qa_preservation VALUES (?)',('Synthetic data must survive uninstall.',))
        database.close()
        report['profile_fixture']='synthetic_files_only_no_application_session'
        report['screen_observation_started']=False
    else:
        settings=qa.SettingsManager(profile_dir=profile)
        settings.save_perception_settings(qa.PerceptionSettings(enabled=False,allow_remote=False))
        report['qa_screen_observation_explicitly_disabled']=True
    process=None;installed=False
    env=os.environ.copy()
    for key in tuple(env):
        if any(word in key.upper() for word in ('API_KEY','API_TOKEN','AUTH_TOKEN','SECRET','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')):env.pop(key,None)
    env['PATH']=str(Path(env.get('SystemRoot',r'C:\Windows'))/'System32')+os.pathsep+env.get('SystemRoot',r'C:\Windows')
    try:
        result=subprocess.run([str(installer),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS','/TASKS=',
            '/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS','/LANG=chinesesimp','/DIR='+str(installation),
            '/LOG='+str(OUT/'installer_install.log')],stdin=subprocess.DEVNULL,capture_output=True,timeout=900,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode==0,('Installer failed',result.returncode)
        installed=True
        report['checks']['install_exit_0']=True
        assert registration(),'The installer did not register uninstall support.'
        manifest=json.loads((payload/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
        for row in manifest['files']:
            path=installation/row['path']
            assert path.is_file() and path.stat().st_size==row['bytes'] and sha(path)==row['sha256'],row['path']
        assert sha(installation/'PACKAGE-MANIFEST.json')==sha(payload/'PACKAGE-MANIFEST.json')
        report['checks']['every_installed_manifest_file_sha256_matches']=len(manifest['files'])
        exe=installation/'鲸伴.exe'
        if args.non_interactive:
            report['app_exit_code']=help_probe(exe,work,profile,env,OUT)
            report['checks']['installed_exe_help_exit_0']=True
            report['checks']['owned_profile_unchanged_by_help']=True
        else:
            stdout=(OUT/'installed_stdout.log').open('wb');stderr=(OUT/'installed_stderr.log').open('wb')
            process=subprocess.Popen([str(exe),'--profile-dir',str(profile),'--qa-exit-after-ms','120000'],cwd=str(work),
                env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            qa.find_window(process,'鲸伴')
            activation=subprocess.run([str(exe),'--profile-dir',str(profile)],cwd=str(work),env=env,
                stdin=subprocess.DEVNULL,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert activation.returncode==0
            chat=qa.find_window(process,'和鲸鱼娘聊天 - 鲸伴')
            qa.wait(lambda:qa.named(process,chat,'发送给鲸伴的消息','ControlType.Edit'),description='installed WebEngine ready')
            assert qa.named(process,chat,'鲸伴','ControlType.Document')
            report['checks']['installed_exe_native_webengine']=True
            request={'version':1,'action':'ask_selection','context':{'text':'已安装版本的合成选区','title':'安装器测试',
                'url':'https://example.invalid/installed','page_text':'仅做本地打包验收。','app_name':'Acceptance fixture'}}
            data=json.dumps(request,ensure_ascii=False).encode('utf-8')
            bridge=subprocess.run([str(installation/'鲸伴网页桥.exe'),'--config-path',str(profile/'settings.ini'),
                'chrome-extension://gbeikpndpckhjeakbckijelidpdpinei/'],input=struct.pack('<I',len(data))+data,
                capture_output=True,env=env,cwd=str(work),timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert bridge.returncode==0 and len(bridge.stdout)>=4
            length=struct.unpack('<I',bridge.stdout[:4])[0]
            assert len(bridge.stdout)==length+4 and json.loads(bridge.stdout[4:]).get('ok') is True
            report['checks']['installed_bridge_raw_frame']=True
            pets=[row for row in qa.windows(process.pid) if row['title']=='鲸伴' and row['owner']==0]
            assert pets
            qa.uia(process,max(pets,key=lambda row:row['area']),'close')
            assert process.wait(timeout=30)==0
            report['checks']['normal_exit_0']=True
            stdout.close();stderr.close()
            report['interactive_ui_test']='passed'
        assert (profile/'settings.ini').is_file() and (profile/'conversations.sqlite3').is_file()
        before_data={p.name:sha(p) for p in (marker,profile/'settings.ini',profile/'conversations.sqlite3')}
        optional_background_checks(args,installation,work,OUT,report)
        uninstaller=installation/'unins000.exe';assert uninstaller.is_file()
        result=subprocess.run([str(uninstaller),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
            '/LOG='+str(OUT/'installer_uninstall.log')],cwd=str(work),stdin=subprocess.DEVNULL,capture_output=True,
            timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode==0
        wait(lambda:not exe.exists(),seconds=30,description='uninstaller removes executable')
        wait(lambda:not registration(),seconds=15,description='uninstall registration removed')
        assert all((profile/name).is_file() and sha(profile/name)==digest for name,digest in before_data.items())
        report['checks']['uninstall_exit_0_and_executable_removed']=True
        report['checks']['user_profile_retained_unchanged']=True
        assert (sha(shortcut) if shortcut.is_file() else None)==shortcut_before
        assert startup_digest()==startup_before
        report['checks']['existing_user_shortcut_and_startup_unchanged']=True
        report['success']=True
    except BaseException as error:
        report['error']={'type':type(error).__name__,'message':str(error)[-3000:]}
        raise
    finally:
        if not args.non_interactive and process is not None and process.poll() is None:
            pets=[row for row in qa.windows(process.pid) if row['title']=='鲸伴' and row['owner']==0]
            if pets:
                qa.uia(process,max(pets,key=lambda row:row['area']),'close')
                try:process.wait(timeout=30)
                except subprocess.TimeoutExpired:report['normal_exit_timed_out']=True
        if process is not None: report['app_exit_code']=process.returncode
        report.setdefault('app_exit_code',None)
        report['app_registration_remaining']=registration()
        (OUT/'installer_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True),flush=True)

if __name__=='__main__':main()
