"""Metadata-only QA entry around the actual production worker, no policy override."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"agent_backend/nanobot"))


def main():
    target=Path(os.environ.pop("AURA_QA_PROJECT_PROOF"))
    parent=(ROOT/"agent_workspace").resolve()
    if not target.is_absolute() or not target.resolve().is_relative_to(parent) or target.exists():return 2
    import worker,mxc_native
    original=mxc_native.run;events=[]
    def observed(*args,**kwargs):
        value=original(*args,**kwargs)
        argv=args[0]
        phase="runtime_version" if list(argv)[1:]==["--version"] else "workload" if "sandbox" in argv and "--include-managed-config" in argv else "unknown"
        events.append({"code":value.code,"tree_reaped":value.tree_reaped is True,"resources_closed":value.resources_closed is True,
            "limits_verified":value.limits_verified is True,"cleanup_lease_absent":value._cleanup_lease is None,"resumed":value.resumed is True,"phase":phase})
        return value
    mxc_native.run=observed
    try:asyncio.run(worker.main())
    finally:target.write_text(json.dumps({"schema":"aura.project_native_qa.v1","native":events}),encoding="utf-8")
    return 0


if __name__=="__main__":raise SystemExit(main())
