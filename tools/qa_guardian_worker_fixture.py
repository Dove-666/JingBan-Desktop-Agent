"""Trusted stdlib worker and isolated host-adapter canary, never model code."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def output(value):
    # -I intentionally ignores PYTHONIOENCODING. Own the fixture's wire
    # encoding explicitly rather than depending on the Windows console locale.
    sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def worker(arguments):
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if arguments.stderr_bytes:
        sys.stderr.buffer.write(b"S" * arguments.stderr_bytes)
        sys.stderr.buffer.flush()
    output({"type": "ready", "protocol": 1, "sdk": "nanobot-ai", "sdk_version": "0.4.0"})
    if arguments.no_read:
        time.sleep(20)
        return 0
    raw = sys.stdin.buffer.readline()
    if not raw.endswith(b"\n"):
        return 2
    value = json.loads(raw)
    if set(value) != {"fixture", "text", "spawn_child"} or value["fixture"] != "trusted-fixed-worker":
        return 3
    from desktop_pet.agents.guardian_native import Native
    native = Native()
    inherited = bool(native.k.SetEvent(arguments.canary)) if arguments.canary else False
    Path(arguments.marker).write_text("DISPATCHED", encoding="ascii")
    result = {"type": "result", "text": value["text"], "canary_inherited": inherited}
    sys.stderr.write("fixed stderr synthetic marker\n")
    sys.stderr.flush()
    if value["spawn_child"]:
        child = subprocess.Popen([sys.executable, "-I", "-u", str(Path(__file__).resolve()), "grandchild"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=0x08000000)
        result.update(child_pid=child.pid, child_creation_time=native.creation_time(int(child._handle)))
    output(result)
    # EOF after abrupt host death does not claim the grandchild stopped.
    # A guardian must still query Job zero independently.
    sys.stdin.buffer.readline()
    return 0


def read_frame(process, timeout=5):
    end, raw = time.monotonic() + timeout, b""
    while time.monotonic() < end:
        raw += process.read_available()
        if b"\n" in raw:
            line, extra = raw.split(b"\n", 1)
            if extra:
                raise ValueError("FIXTURE_EXTRA_FRAME")
            return json.loads(line.decode("utf-8"))
        if process.poll() is not None:
            raise ValueError("FIXTURE_EXITED_BEFORE_FRAME")
        time.sleep(.01)
    raise ValueError("FIXTURE_FRAME_TIMEOUT")


def parent(arguments):
    from desktop_pet.agents.guardian_native import Native
    from desktop_pet.agents.guardian_worker import GuardianWorker, PrivateGuardianLocation, consume_restarted_stopped, pending_cleanup_count
    from desktop_pet.agents.process_lifecycle import WorkerLifecycle
    from desktop_pet.agents.job_queue import HostJobReference
    from desktop_pet.agents.staged_input import StagedRunInput
    native = Native()
    location = PrivateGuardianLocation.prepare(arguments.config, model_scopes=(arguments.workspace,), native=native)
    lifecycle = WorkerLifecycle()
    reference = HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64)
    process, handle, writer, phases = None, None, None, []
    attempted = []
    cancelled = threading.Event()
    canary = native.k.CreateEventW(None, True, False, None)
    native.k.SetHandleInformation(canary, 1, 1)
    marker = Path(arguments.workspace) / "dispatch-marker.txt"
    try:
        def registered(current):
            nonlocal handle
            handle = lifecycle.register(current, str(uuid4()), reference.conversation_id)
            lifecycle.attach_tree(handle, current.tree)
        process = GuardianWorker.create([sys.executable, "-I", "-u", str(Path(__file__).resolve()), "worker",
            "--marker", str(marker), "--canary", str(canary)], cwd=REPO,
            location=location, job_id=str(uuid4()), reference=reference, installation_id="b" * 64,
            launch_epoch="c" * 64, launch_attempt_callback=lambda: attempted.append(True),
            lifecycle_callback=registered, phase_callback=phases.append)
        base = {"manifest": process.manifest, "root_pid": process.pid,
            "root_creation_time": native.creation_time(process._handle), "guardian_pid": process.guardian.process.pid,
            "guardian_creation_time": native.creation_time(process.guardian.process.handle),
            "launch_attempt_count": len(attempted), "same_lifecycle_object": handle._process is process}
        if arguments.pause == "armed":
            output({**base, "phase": "armed", "phases": phases, "root_resumed": process._root.resumed})
            if sys.stdin.readline() != "RESUME\n":
                return 0
        process.resume()
        ready = read_frame(process)
        if ready != {"type": "ready", "protocol": 1, "sdk": "nanobot-ai", "sdk_version": "0.4.0"}:
            raise ValueError("FIXTURE_READY_INVALID")
        writer = StagedRunInput(process.stdin, stop_owned=lambda: lifecycle.request_stop(handle), cancellation_event=cancelled)
        process.bind_writer(writer)
        writer.start()
        writer.stage(json.dumps({"fixture": "trusted-fixed-worker", "text": "Unicode 🌊 中文", "spawn_child": True},
                                ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        output({**base, "phase": "staged", "phases": phases, "marker_exists": marker.exists()})
        command = sys.stdin.readline()
        if command == "COMMIT\n":
            with threading.Lock():
                if cancelled.is_set():
                    raise ValueError("FIXTURE_REVOKED")
                process.validate_private_scope()
                lifecycle.mark_dispatched(handle)
                writer.commit()
            if not writer.close():
                raise ValueError("FIXTURE_WRITER_UNCONFIRMED")
            result = read_frame(process)
            output({**base, "phase": "committed", "result": result, "marker_exists": marker.exists(),
                    "ambient_event_signaled": native.signaled(canary)})
            command = sys.stdin.readline()
        elif command == "REVOKE\n":
            cancelled.set()
        if command in {"STOP\n", "REVOKE\n"}:
            receipt = lifecycle.settle(handle, timeout_seconds=3)
            durable = consume_restarted_stopped(location, process.manifest)
            output({"phase": "closed", "same_lifecycle_object": handle._process is process,
                "closure_valid": lifecycle.validates(handle, receipt), "durable_stopped": durable is not None,
                "marker_exists": marker.exists(), "pending_count": pending_cleanup_count(),
                "stdio_closed": all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))})
        return 0
    finally:
        if writer is not None: writer.close()
        if process is not None: process.cleanup(timeout_seconds=3)
        native.close(canary)
        location.close()


def close_fault(arguments):
    from desktop_pet.agents.guardian_worker import GuardianWorker, PrivateGuardianLocation, pending_cleanup_count, reconcile_cleanup
    from desktop_pet.agents.guardian_native import GuardianError
    from desktop_pet.agents.job_queue import HostJobReference
    location = PrivateGuardianLocation.prepare(arguments.config, model_scopes=(arguments.workspace,))
    reference = HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64)
    def create():
        return GuardianWorker.create([sys.executable, "-I", "-u", str(Path(__file__).resolve()), "worker",
            "--marker", str(Path(arguments.workspace) / "unused-marker.txt")], cwd=REPO,
            location=location, job_id=str(uuid4()), reference=reference, installation_id="b" * 64,
            launch_epoch="c" * 64, launch_attempt_callback=lambda: None, lifecycle_callback=lambda _: None)
    process = create()
    root, job = process._root.handle, process._job.handle
    original = process.stdout._file
    class CloseErrorAfterClosed:
        closed = False
        def close(self):
            original.close()
            self.closed = True
            raise OSError("fixed close failure after FileIO closed")
    process.stdout._file = CloseErrorAfterClosed()
    first = process.cleanup(timeout_seconds=2)
    second = process.cleanup(timeout_seconds=2)
    reconciled = reconcile_cleanup(timeout_seconds=.2)
    busy = False
    try: create()
    except GuardianError as exc: busy = exc.args[0] == "GUARDIAN_WORKER_BUSY"
    output({"first_confirmed": first, "second_confirmed": second, "reconcile_confirmed": reconciled,
        "registry_busy": busy, "pending_count": pending_cleanup_count(),
        "kept_exact_identity": process._root.handle == root and process._job.handle == job})
    location.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["parent", "worker", "grandchild", "close_fault"])
    parser.add_argument("--config")
    parser.add_argument("--workspace")
    parser.add_argument("--marker")
    parser.add_argument("--canary", type=int, default=0)
    parser.add_argument("--no-read", action="store_true")
    parser.add_argument("--stderr-bytes", type=int, default=0)
    parser.add_argument("--pause", choices=["armed", "staged"], default="staged")
    args = parser.parse_args()
    if args.role == "grandchild":
        time.sleep(15)
    elif args.role == "worker":
        raise SystemExit(worker(args))
    elif args.role == "close_fault":
        raise SystemExit(close_fault(args))
    else:
        raise SystemExit(parent(args))
