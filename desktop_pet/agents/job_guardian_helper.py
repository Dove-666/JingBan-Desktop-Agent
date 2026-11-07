"""Private stdlib-only exact-handle guardian entrypoint; never model input."""
from __future__ import annotations

import base64
import ctypes as C
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guardian_native import Native, GuardianError
from job_guardian import PrivateRunStore, canonical, decode, _mac


def main():
    if len(sys.argv) != 7:
        return 2
    try:
        handles = [int(value) for value in sys.argv[1:]]
        if any(value <= 0 for value in handles) or len(set(handles)) != 6:
            return 2
        boot, ack, job, root, app, stop = handles
        native = Native()
        for handle in handles:
            native.noninherit(handle)
        bootstrap = decode(native.read_bounded(boot, maximum=4096, timeout=5))
        if (set(bootstrap) != {"schema", "manifest", "manifest_sha256", "key", "private_directory"}
                or bootstrap.get("schema") != "aura.guardian_bootstrap.v1"
                or not isinstance(bootstrap.get("private_directory"), str)
                or not isinstance(bootstrap.get("key"), str)):
            return 3
        store = PrivateRunStore(bootstrap["private_directory"], native)
        try:
            manifest, digest, key = store.identity()
            if (manifest != bootstrap["manifest"] or digest != bootstrap["manifest_sha256"]
                    or key != base64.b64decode(bootstrap["key"], validate=True)
                    or native.creation_time(root) != manifest["process_creation_time"]
                    or not native.in_job(root, job) or native.in_job(native.k.GetCurrentProcess(), job)):
                return 4
            armed = {"schema": "aura.guardian_armed.v1", "manifest_sha256": digest,
                "run_nonce": manifest["run_nonce"], "guardian_in_external_job": native.in_job(native.k.GetCurrentProcess())}
            armed["hmac_sha256"] = _mac(key, armed)
            native.write(ack, canonical(armed) + b"\n")
            native.close(boot)
            native.close(ack)
            waiting = (C.c_void_p * 2)(app, stop)
            result = native.k.WaitForMultipleObjects(2, waiting, False, 0xFFFFFFFF)
            if result not in {0, 1}:
                return 5
            native.check(native.k.TerminateJobObject(job, 77), "GUARDIAN_JOB_STOP_FAILED")
            if not native.confirm_closed(job, root, timeout=5):
                return 6
            store.write_closure(manifest, digest, key, trigger="app_exit" if result == 0 else "stop")
            return 0
        finally:
            store.close()
    except GuardianError as exc:
        return {"GUARDIAN_RECORD_CREATE_FAILED": 21, "GUARDIAN_FLUSH_FAILED": 22,
            "GUARDIAN_RECEIPT_PUBLISH_FAILED": 23, "GUARDIAN_JOB_QUERY_FAILED": 24,
            "GUARDIAN_JOB_STOP_FAILED": 25, "GUARDIAN_RECORD_UNAVAILABLE": 26,
            "GUARDIAN_KEY_UNAVAILABLE": 27, "GUARDIAN_RECEIPT_BINDING_INVALID": 28,
            "GUARDIAN_RECORD_INVALID": 29}.get(exc.args[0] if exc.args else None, 7)
    except Exception:
        # Exit codes and absence of an authenticated receipt are the only
        # failure channel. No raw error, path, key or child output is printed.
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
