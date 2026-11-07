"""Existing-csc fixed helper experiment; never installs or enables anything."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.audit_mxc_python_mitigation import LAUNCH
from agent_backend.nanobot import mxc_execution as mxc,mxc_native as native
from agent_backend.nanobot.container_execution import HostExecutionScope
from agent_backend.nanobot.safe_files import WorkspaceFiles

SOURCE=r'''using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
class FixedProbe {
 [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessMitigationPolicy(IntPtr p,int kind,out UInt32 value,UIntPtr n);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetProcessMitigationPolicy(int kind,ref UInt32 value,UIntPtr n);
 static string B(bool b){return b?"true":"false";}
 static int Main(string[] args){
  if(args.Length!=1)return 7;
  UInt32 before=0,after=0,zero=0;
  bool query=GetProcessMitigationPolicy(GetCurrentProcess(),4,out before,(UIntPtr)4);
  bool clear=SetProcessMitigationPolicy(4,ref zero,(UIntPtr)4);
  int error=clear?0:Marshal.GetLastWin32Error();
  bool queried=GetProcessMitigationPolicy(GetCurrentProcess(),4,out after,(UIntPtr)4);
  string value="{\"query\":"+B(query)+",\"disabled_before\":"+B((before&1)!=0)+",\"clear_returned\":"+B(clear)+",\"clear_error\":"+error+",\"query_after\":"+B(queried)+",\"disabled_after\":"+B((after&1)!=0)+"}";
  File.WriteAllText(args[0],value,new UTF8Encoding(false));return 0;
 }
}
'''


def main():
    if sys.argv[1:]!=['--fixed-dotnet-mitigation']:
        print(json.dumps({'code':'FIXED_DOTNET_MITIGATION_NOT_REQUESTED'}));return 0
    compiler=Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    paths=['tools/audit_mxc_dotnet_mitigation.py','tools/audit_mxc_python_mitigation.py',
        'agent_backend/nanobot/mxc_native.py','agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_entry.py']
    hashes=lambda:{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    source=hashes();report={'schema':'aura.qa.fixed_dotnet_mitigation.v1','source_sha256':source,
        'production_enabled':False,'fixed_trusted_payload_only':True,'runtime_scope_expanded':False}
    records=[]
    with tempfile.TemporaryDirectory(prefix='mxc-dotnet-owned-',dir=ROOT/'agent_workspace') as temp:
        root=Path(temp);workspace=root/'workspace';scratch=root/'scratch';workspace.mkdir();scratch.mkdir()
        cs=root/'fixed.cs';exe=root/'fixed.exe';cs.write_text(SOURCE,encoding='utf-8')
        compile=subprocess.run([str(compiler),'/nologo','/target:exe','/platform:x64','/out:'+str(exe),str(cs)],
            capture_output=True,timeout=15,creationflags=0x08000000,
            env={'SystemRoot':os.environ['SystemRoot'],'TEMP':str(root),'TMP':str(root)})
        report['compiler_exit_zero']=compile.returncode==0
        report['compiler_sha256']=hashlib.sha256(compiler.read_bytes()).hexdigest()
        if compile.returncode!=0:raise ValueError('FIXED_HELPER_COMPILATION_FAILED')
        report['helper_sha256']=hashlib.sha256(exe.read_bytes()).hexdigest()
        runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch,
            policy=replace(mxc.MxcPolicy(),timeout_seconds=8,cpu_seconds=3,stdout_limit=4096))
        def run(*a,**kw):
            r=native.run(*a,**kw);records.append({'code':r.code,'exit_code':r.exit_code,'tree_reaped':r.tree_reaped,
                'resources_closed':r.resources_closed,'limits_verified':r.limits_verified});return r
        runner._native_run=run
        def stage(directory,scope):shutil.copyfile(exe,directory/'fixed.exe')
        scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'fixed-dotnet').hexdigest(),is_current=lambda:True)
        code=LAUNCH+'\nout={"mitigated":launch("",1<<28,"dotnet-on.json",os.path.join(INPUT_DIR,"fixed.exe"),[])}\n'
        code+='out["unmitigated_control"]=launch("",2<<28,"dotnet-off.json",os.path.join(INPUT_DIR,"fixed.exe"),[])\nprint(json.dumps(out))\n'
        result=runner.execute(code,scope=scope,stage_inputs=stage)
        report['result']=json.loads(result.to_json());report['native_invocations']=records
        report['native_pending_cleanup']=native.has_pending_cleanup()
        if native.has_pending_cleanup() or any(not r['tree_reaped'] or not r['resources_closed'] for r in records):
            raise ValueError('FIXED_NATIVE_CLOSURE_UNCONFIRMED')
    report['owned_directory_removed']=not root.exists()
    report['exit_sha256']=hashes();report['source_unchanged']=source==report['exit_sha256']
    (ROOT/'artifacts/mxc-dotnet-creation-mitigation-probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));return 0


if __name__=='__main__':raise SystemExit(main())
