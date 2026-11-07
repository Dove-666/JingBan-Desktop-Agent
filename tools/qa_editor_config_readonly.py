"""Opt-in, getter-only audit of the existing configured QA loader.

No native window, model call, save, settings copy, permission change, or user
application control. Values and credentials never enter diagnostic output.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


class WriteDenied(RuntimeError):pass


class Audit:
    def __init__(self):
        from desktop_pet.agents.settings import AgentSettings
        self.allowed_keys={"api/base_url","api/model","api/timeout_seconds","api/reasoning_effort"}
        self.allowed_keys.update("agents/"+field.name for field in fields(AgentSettings))
        self.reads=Counter();self.denied=[];self.vault_reads=0
    def key(self,value):return value if type(value) is str and value in self.allowed_keys else "OTHER_KEY"
    def deny(self,operation,key=None):
        self.denied.append({"operation":operation,"key":self.key(key) if key is not None else "NO_KEY"})
        raise WriteDenied("CONFIG_WRITE_ATTEMPT_DENIED")


class GetterSettings:
    """Closed getter facade. It does not expose the underlying QSettings."""
    __slots__=("__store","__audit")
    def __init__(self,store,audit):self.__store,self.__audit=store,audit
    def value(self,key,*args,**kwargs):
        self.__audit.reads["value:"+self.__audit.key(key)]+=1
        return self.__store.value(key,*args,**kwargs)
    def contains(self,key):
        self.__audit.reads["contains:"+self.__audit.key(key)]+=1
        return self.__store.contains(key)
    def fileName(self):
        self.__audit.reads["fileName"]+=1
        return self.__store.fileName()
    def allKeys(self):
        self.__audit.reads["allKeys"]+=1
        return self.__store.allKeys()
    def status(self):
        self.__audit.reads["status"]+=1
        return self.__store.status()
    def setValue(self,key,value):self.__audit.deny("SET_VALUE",key)
    def remove(self,key):self.__audit.deny("REMOVE",key)
    def clear(self):self.__audit.deny("CLEAR")
    def sync(self):self.__audit.deny("SYNC")
    def beginGroup(self,key):self.__audit.deny("UNSUPPORTED_MUTATING_VIEW",key)
    def endGroup(self):self.__audit.deny("UNSUPPORTED_MUTATING_VIEW")
    def __getattr__(self,name):self.__audit.deny("UNSUPPORTED_SETTINGS_METHOD")


class GetterVault:
    __slots__=("__backend","__audit")
    def __init__(self,backend,audit):self.__backend,self.__audit=backend,audit
    def get_password(self,service,account):
        self.__audit.vault_reads+=1
        return self.__backend.get_password(service,account)
    def set_password(self,*args,**kwargs):self.__audit.deny("VAULT_SET")
    def delete_password(self,*args,**kwargs):self.__audit.deny("VAULT_DELETE")
    def __getattr__(self,name):self.__audit.deny("UNSUPPORTED_VAULT_METHOD")


def descriptor(path):
    value=path.stat()
    return {"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"bytes":value.st_size,
        "mtime_ns":value.st_mtime_ns,"mtime_utc":datetime.fromtimestamp(value.st_mtime,timezone.utc).isoformat()}


def configured_audit():
    from PySide6.QtCore import QCoreApplication,QStandardPaths
    from desktop_pet import settings_manager as module
    from tools.run_agent_benchmarks import ConfiguredExecutor
    app=QCoreApplication.instance() or QCoreApplication([]);app.setApplicationName("AURA Desktop Pet")
    path=Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))/"settings.ini"
    audit=Audit();before=descriptor(path);original=module.SettingsManager
    class GetterManager(original):
        def __init__(self,settings=None,credential_store=None):
            if settings is None:raise WriteDenied("CONFIG_GETTER_STORE_REQUIRED")
            credentials=credential_store or module.CredentialStore()
            if credentials._backend is not None:credentials._backend=GetterVault(credentials._backend,audit)
            super().__init__(GetterSettings(settings,audit),credential_store=credentials)
    succeeded=False;code=None
    try:
        with patch.object(module,"SettingsManager",GetterManager):
            value=ConfiguredExecutor(30)
            succeeded=bool(value.api and value.settings and value.key)
            # Release captured values without serializing/hash-printing keys.
            value=None
    except WriteDenied:code="CONFIG_WRITE_ATTEMPT_DENIED"
    except Exception:code="CONFIG_READ_FAILED"
    after=descriptor(path)
    paths=[ROOT/"tools/run_agent_benchmarks.py",ROOT/"desktop_pet/settings_manager.py",ROOT/"desktop_pet/agents/settings.py",Path(__file__)]
    return {"schema":"aura.configured_loader_getter_audit.v1","created_at":datetime.now(timezone.utc).isoformat(),
        "loader_succeeded":succeeded,"code":code,"getter_calls":dict(audit.reads),"vault_get_count":audit.vault_reads,
        "write_attempts_denied":audit.denied,"settings_file_before":before,"settings_file_after":after,
        "settings_bytes_unchanged_during_this_audit":before["sha256"]==after["sha256"],
        "source_hashes":{str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "model_called":False,"native_input":False,"private_values_or_keys_saved":False,"original_failure_overridden":False,
        "scope":"Getter-only current loader audit. Cannot recover which field or writer changed the original run's settings file; no per-key historical snapshot exists."}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--configured",action="store_true")
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/owned-editor-model/config-getter-audit.json")
    args=parser.parse_args()
    if not args.configured:print(json.dumps({"status":"not_run","model_called":False,"credentials_read":False}));return 0
    output=args.output.resolve()
    if not output.is_relative_to((ROOT/"artifacts").resolve()):parser.error("Output must be inside artifacts")
    value=configured_audit();output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(value,indent=2),encoding="utf-8")
    print(json.dumps({"loader_succeeded":value["loader_succeeded"],"write_attempt_count":len(value["write_attempts_denied"]),
        "settings_bytes_unchanged_during_this_audit":value["settings_bytes_unchanged_during_this_audit"],"model_called":False}))
    return 0 if value["loader_succeeded"] and not value["write_attempts_denied"] else 1


if __name__=="__main__":raise SystemExit(main())
