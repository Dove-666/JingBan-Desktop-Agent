"""Fixed owned parent/worker/grandchild fixture. No model or user documents."""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from desktop_pet.agents.guardian_native import Native, OwnedJob, OwnedProcess, GuardianError
from desktop_pet.agents.job_guardian import GuardianSession, PrivateRunStore, new_manifest

SYNTHETIC_OUTPUT = "fixture-worker-output-not-for-guardian-7c42"


def output(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def worker(mode, canary):
    native = Native()
    # The parent deliberately leaves this event inheritable. The explicit
    # worker whitelist must still exclude it. This is no authority grant.
    inherited_canary = bool(native.k.SetEvent(canary)) if canary else False
    if sys.stdin.readline() != "GO\n":
        return 2
    child = subprocess.Popen([sys.executable, "-I", "-u", str(Path(__file__).resolve()), "grandchild"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=0x08000000)
    output({"child_pid": child.pid, "child_creation_time": native.creation_time(int(child._handle)),
            "worker_output": SYNTHETIC_OUTPUT, "canary_inherited": inherited_canary})
    if mode == "root_exit":
        os._exit(0)
    end = time.monotonic() + 20
    while time.monotonic() < end:
        time.sleep(.1)
    return 0


def parent(root, mode):
    native = Native()
    job = OwnedJob(native)
    pipes = []
    process = guardian = store = None
    try:
        stdin_read, stdin_write = native.pipe()
        stdout_read, stdout_write = native.pipe()
        pipes = [stdin_read, stdin_write, stdout_read, stdout_write]
        null = native.k.CreateFileW("NUL", 0xC0000000, 3, None, 3, 0, None)
        pipes.append(null)
        canary = native.k.CreateEventW(None, True, False, None)
        pipes.append(canary)
        native.k.SetHandleInformation(canary, 1, 1)
        process = OwnedProcess.create(native, [sys.executable, "-I", "-u", str(Path(__file__).resolve()),
            "worker", "--mode", mode, "--canary", str(canary)], cwd=REPO,
            inherited=[stdin_read, stdout_write, null], stdio=(stdin_read, stdout_write, null))
        job.assign(process)
        creation = native.creation_time(process.handle)
        base = {"root_pid": process.pid, "root_creation_time": creation}
        if mode == "before_manifest":
            output({**base, "phase": "assigned"})
        else:
            manifest = new_manifest(job_id=str(uuid4()), conversation_id=str(uuid4()), generation=str(uuid4()),
                operation_id=str(uuid4()), installation_id="1" * 64, launch_epoch="2" * 64,
                process_creation_time=creation)
            store = PrivateRunStore.create(root, manifest, native)
            base["manifest"] = manifest
            if mode == "before_arm":
                output({**base, "phase": "assigned"})
            else:
                if mode in {"query_denied", "terminate_denied"}:
                    duplicate = native.duplicate
                    def restricted(handle, access, **kwargs):
                        if handle == job.handle:
                            access &= ~(4 if mode == "query_denied" else 8)
                        return duplicate(handle, access, **kwargs)
                    native.duplicate = restricted
                try:
                    guardian = GuardianSession.arm(store, job=job, root=process)
                except GuardianError:
                    if mode != "query_denied":
                        raise
                    output({**base, "phase": "arm_failed", "root_resumed": process.resumed})
                else:
                    base.update(guardian_pid=guardian.process.pid,
                        guardian_creation_time=native.creation_time(guardian.process.handle),
                        guardian_in_external_job=guardian.guardian_in_external_job)
                    if mode == "before_resume":
                        output({**base, "phase": "armed"})
                    else:
                        process.resume()
                        native.write(stdin_write, b"GO\n")
                        child = json.loads(native.read_bounded(stdout_read, maximum=2048, timeout=5))
                        base.update(grandchild_pid=child["child_pid"], grandchild_creation_time=child["child_creation_time"],
                            canary_inherited=child["canary_inherited"], ambient_event_signaled=native.signaled(canary))
                        # Do not forward the worker's synthetic secret or output.
                        output({**base, "phase": "running"})
        # The independent test either kills this synthetic App abruptly or
        # asks for a normal Stop. No interpreter atexit is needed for proof.
        command = sys.stdin.readline()
        if command in {"STOP\n", "STOP_KEEP\n"} and guardian is not None:
            guardian.stop()
            native.k.WaitForSingleObject(guardian.process.handle, 6000)
            if command == "STOP_KEEP\n":
                sys.stdin.readline()
        return 0
    finally:
        if guardian is not None:
            guardian.close()
        if store is not None:
            store.close()
        job.terminate()
        if process is not None:
            native.k.WaitForSingleObject(process.handle, 2000)
            process.close()
        job.close()
        for handle in pipes:
            native.close(handle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["parent", "worker", "grandchild"])
    parser.add_argument("--root")
    parser.add_argument("--mode", default="running")
    parser.add_argument("--canary", type=int, default=0)
    arguments = parser.parse_args()
    if arguments.role == "grandchild":
        time.sleep(15)
    elif arguments.role == "worker":
        raise SystemExit(worker(arguments.mode, arguments.canary))
    else:
        raise SystemExit(parent(Path(arguments.root), arguments.mode))
