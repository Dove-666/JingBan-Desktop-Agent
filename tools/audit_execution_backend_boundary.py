"""Fixed trusted MXC canary only; no model code, credentials or engine startup.

Creates and removes one uniquely owned synthetic HKCU leaf and two temporary
ordinary files. No registry enumeration or other registry value read exists.
The existing backend remains disabled; this explicit QA invocation opts in only
the fixed code below and writes a bounded bool/hash/fixed-code report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

_RETAINED_QA_OWNERS = []
FIXED_CODE = """import json,os,winreg
out={}
for name,path in [('original_workspace_read',input['workspace_file']),('owned_private_file_read',input['private_file'])]:
    try:
        with open(path,'r',encoding='utf-8') as stream: data=stream.read(1024)
        out[name+'_open']=True
        out[name]=data==input['marker']
        out[name+'_errno']=0
        out[name+'_winerror']=0
    except OSError as error:
        out[name]=False
        out[name+'_open']=False
        out[name+'_errno']=error.errno or 0
        out[name+'_winerror']=error.winerror or 0
try:
    os.link(input['private_file'],'owned-hardlink-canary.txt')
    out['owned_private_hardlink_created']=True
    with open('owned-hardlink-canary.txt','r',encoding='utf-8') as stream: data=stream.read(1024)
    out['owned_private_hardlink_read']=data==input['marker']
    out['owned_private_hardlink_errno']=0
except OSError as error:
    out['owned_private_hardlink_created']=False
    out['owned_private_hardlink_read']=False
    out['owned_private_hardlink_errno']=error.errno or 0
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,input['registry_leaf'],0,winreg.KEY_READ) as key:
        value,kind=winreg.QueryValueEx(key,'SyntheticMarker')
    out['owned_registry_read']=value==input['marker'] and kind==winreg.REG_SZ
    out['owned_registry_read_winerror']=0
except OSError as error:
    out['owned_registry_read']=False
    out['owned_registry_read_winerror']=error.winerror or 0
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,input['registry_leaf'],0,winreg.KEY_QUERY_VALUE) as key:
        value,kind=winreg.QueryValueEx(key,'SyntheticMarker')
    out['owned_registry_query_only']=value==input['marker'] and kind==winreg.REG_SZ
    out['owned_registry_query_only_winerror']=0
except OSError as error:
    out['owned_registry_query_only']=False
    out['owned_registry_query_only_winerror']=error.winerror or 0
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,input['registry_leaf'],0,winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key,'OwnedWriteCanary',0,winreg.REG_SZ,'fixed-synthetic-write')
    out['owned_registry_write']=True
    out['owned_registry_write_winerror']=0
except OSError as error:
    out['owned_registry_write']=False
    out['owned_registry_write_winerror']=error.winerror or 0
print(json.dumps(out,sort_keys=True))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixed-mxc-canary", action="store_true")
    args = parser.parse_args()
    if not args.fixed_mxc_canary:
        print(json.dumps({"code": "FIXED_CANARY_NOT_REQUESTED", "model_code_enabled": False}))
        return 0
    import winreg
    from agent_backend.nanobot import mxc_execution as mxc
    from agent_backend.nanobot.container_execution import HostExecutionScope
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    parent = ROOT / "agent_workspace"; parent.mkdir(exist_ok=True)
    owner = tempfile.TemporaryDirectory(prefix="execution-boundary-owned-", dir=parent)
    root = Path(owner.name)
    workspace, scratch = root / "workspace", root / "scratch"
    workspace.mkdir(); scratch.mkdir()
    marker = "AURA_OWNED_SYNTHETIC_CANARY_" + uuid4().hex
    workspace_file, private_file = workspace / "source.txt", root / "private.txt"
    workspace_file.write_text(marker, encoding="utf-8"); private_file.write_text(marker, encoding="utf-8")
    leaf = "Software\\AURA_Execution_QA_" + uuid4().hex
    created = False
    runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=scratch)
    scope = HostExecutionScope(str(uuid4()), str(uuid4()), str(uuid4()), WorkspaceFiles(workspace),
        grant_revision=hashlib.sha256(b"fixed-no-added-grants").hexdigest(), is_current=lambda: True)
    report = {"version": 1, "fixed_trusted_code": True, "model_calls": False, "model_code_enabled": False,
        "source_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ("agent_backend/nanobot/mxc_execution.py", "agent_backend/nanobot/mxc_native.py", "agent_backend/nanobot/mxc_entry.py")},
        "runtime_pins": {"codex_sha256": mxc.CODEX_SHA256, "python_sha256": mxc.PYTHON_SHA256},
        "canary_identity_sha256": hashlib.sha256(leaf.encode()).hexdigest(), "backend_default_enabled": mxc.MxcPythonRunner().live_enabled,
        "known_registry_leaf_only": True, "registry_enumerated": False, "real_credentials_read": False,
        "engine_or_vm_started": False, "image_pulled": False}
    clean = False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, leaf, 0, winreg.KEY_SET_VALUE) as key:
            created = True
            winreg.SetValueEx(key, "SyntheticMarker", 0, winreg.REG_SZ, marker)
        value = runner.execute(FIXED_CODE, {"workspace_file": str(workspace_file), "private_file": str(private_file),
            "marker": marker, "registry_leaf": leaf}, scope=scope)
        report["result"] = json.loads(value.to_json())
        report["owned_files_unchanged"] = workspace_file.read_text(encoding="utf-8") == marker and private_file.read_text(encoding="utf-8") == marker
        clean = value.tree_reaped and value.scratch_removed
    except Exception as error:
        text = str(error)
        report["failure_code"] = text if text.startswith("MXC_") and len(text) < 128 and text.replace("_", "").isalnum() else "FIXED_CANARY_FAILED"
    finally:
        if created:
            try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, leaf); report["registry_canary_removed"] = True
            except OSError: report["registry_canary_removed"] = False
        if clean:
            owner.cleanup(); report["owned_directory_removed"] = True
        else:
            _RETAINED_QA_OWNERS.append((owner, runner, scope))
            report["owned_directory_removed"] = False
    (ROOT / "artifacts/execution-backend-fixed-canary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if clean and report.get("registry_canary_removed") and report.get("result", {}).get("code") == "MXC_OK" else 1


if __name__ == "__main__": raise SystemExit(main())
