"""Opt-in native editor roundtrip QA. Default does no UI, IO or model call.

Scripted native UIA is control-chain evidence, never a real-model success rate.
Only the exact spawned Qt window is observable. No desktop enumeration,
screenshots, clipboard, general file dialog or user application is available.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import stat
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
_OWNERS = set()


class EditorQAError(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)


class NativeWindows:
    def __init__(self):
        if os.name != "nt": raise EditorQAError("EDITOR_WINDOWS_REQUIRED")
        import ctypes
        from ctypes import wintypes
        self.c, self.w = ctypes, wintypes
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.SetForegroundWindow.restype = wintypes.BOOL
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def started(self, handle):
        records = [self.w.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(int(handle), *(self.c.byref(value) for value in records)):
            raise EditorQAError("EDITOR_PROCESS_IDENTITY_UNAVAILABLE")
        value = records[0]
        # FILETIME begins in 1601; .NET StartTime UTC ticks begin in year one.
        return str((value.dwHighDateTime << 32) + value.dwLowDateTime + 504911232000000000)

    def hwnd_pid(self, hwnd):
        pid = self.w.DWORD()
        self.user.GetWindowThreadProcessId(int(hwnd), self.c.byref(pid))
        return int(pid.value)

    def foreground(self): return int(self.user.GetForegroundWindow() or 0)

    def prior_focus(self):
        hwnd = self.foreground(); pid = self.hwnd_pid(hwnd)
        handle = self.kernel.OpenProcess(0x1000, False, pid) if pid else None
        if not handle: return None
        try: return {"hwnd": hwnd, "pid": pid, "started": self.started(handle)}
        except EditorQAError: return None
        finally: self.kernel.CloseHandle(handle)

    def restore(self, previous, owned, user_changed):
        if previous is None: return "unavailable"
        current = self.foreground()
        if current == previous["hwnd"]: return "original_already_foreground"
        if user_changed or current not in {*owned, 0}: return "new_external_focus_preserved"
        if self.hwnd_pid(previous["hwnd"]) != previous["pid"]: return "prior_identity_expired"
        handle = self.kernel.OpenProcess(0x1000, False, previous["pid"])
        if not handle: return "prior_identity_expired"
        try:
            if self.started(handle) != previous["started"]: return "prior_identity_expired"
            self.user.SetForegroundWindow(previous["hwnd"])
            return "original_restored" if self.foreground() == previous["hwnd"] else "restore_not_confirmed"
        finally: self.kernel.CloseHandle(handle)


class OwnedEditor:
    def __init__(self, workspace, *, fault="none", watchdog_ms=90000, python=None):
        if _OWNERS: raise EditorQAError("EDITOR_OWNER_UNCONFIRMED_OR_BUSY")
        self.workspace = Path(workspace).resolve()
        parent = (ROOT / "agent_workspace").resolve()
        if self.workspace == parent or not self.workspace.is_relative_to(parent) or not self.workspace.is_dir():
            raise EditorQAError("EDITOR_WORKSPACE_INVALID")
        self.native = NativeWindows()
        self.previous = self.native.prior_focus()
        self.nonce, self.fault, self.watchdog_ms = uuid4().hex, fault, watchdog_ms
        # Windows venv python.exe can be a redirector whose child owns Qt's
        # HWND. Launch the actual host interpreter, rather than weakening the
        # exact Popen PID/HWND identity check to accept an unbound child PID.
        self.python = python or sys._base_executable
        self.process = self.handle = self.window = None
        self.owner = None; self.hwnd = 0; self.decoys = set()
        self.cancel = threading.Event(); self.frames = queue.Queue()
        self.reason = None; self.user_changed = False; self.closing = False
        self.helpers = []; self.operations = []
        self.lifecycle = None
        self.reader = None
        self.act_timeout = None
        self.launch_unknown = False
        self.helper_launch_unknown = False
        self.focus_monitor = None
        self.monitor_finished = threading.Event()

    def stop(self, code):
        if self.reason is None: self.reason = code
        self.cancel.set()

    def guard(self):
        if self.cancel.is_set(): raise EditorQAError(self.reason or "EDITOR_CANCELLED")
        if (self.process is None or self.process.poll() is not None
                or self.native.started(self.process._handle) != self.owner["started"]
                or self.native.hwnd_pid(self.hwnd) != self.process.pid):
            self.stop("EDITOR_IDENTITY_CHANGED"); raise EditorQAError(self.reason)
        if self.native.foreground() != self.hwnd:
            self.user_changed = True
            self.stop("EDITOR_FOREGROUND_MISMATCH"); raise EditorQAError(self.reason)

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode("utf-8") + b"\n"); self.process.stdin.flush()

    def start(self):
        if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
        from desktop_pet.agents.process_lifecycle import WorkerLifecycle
        from desktop_pet.agents.service import _ProcessTree
        self.lifecycle = WorkerLifecycle()
        _OWNERS.add(self)
        try:
            self.process = subprocess.Popen([self.python, "-I", "-B", "-u", str(ROOT / "tools/qa_owned_editor_fixture.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=0x08000000 | 0x00000200, bufsize=0)
        except Exception:
            self.launch_unknown = True
            raise EditorQAError("EDITOR_LAUNCH_UNCONFIRMED") from None
        self.handle = self.lifecycle.register(self.process, "owned-editor-" + self.nonce, None)
        self.lifecycle.attach_tree(self.handle, _ProcessTree(self.process))
        self.lifecycle.mark_dispatched(self.handle)
        self.send({"type": "setup", "nonce": self.nonce, "workspace": str(self.workspace),
            "fault": self.fault, "watchdog_ms": self.watchdog_ms})
        def read():
            try:
                for line in self.process.stdout:
                    if len(line) > 8192: self.stop("EDITOR_PROTOCOL_INVALID"); break
                    value = json.loads(line)
                    if type(value) is not dict or value.get("nonce") != self.nonce:
                        self.stop("EDITOR_PROTOCOL_INVALID"); break
                    if value.get("type") == "editor_decoy": self.decoys.add(value["hwnd"])
                    if value.get("type") in {"editor_closing", "editor_user_input"} and not self.closing:
                        self.stop("EDITOR_USER_STOPPED")
                    self.frames.put(value)
            except Exception: self.stop("EDITOR_PROTOCOL_INVALID")
            finally:
                if not self.closing: self.stop("EDITOR_EXITED")
                self.frames.put({"type": "editor_stream_closed", "nonce": self.nonce})
        self.reader = threading.Thread(target=read, daemon=True, name="owned-editor-output"); self.reader.start()
        frame = self.frames.get(timeout=15)
        if (frame.get("type") != "editor_ready" or frame.get("pid") != self.process.pid
                or type(frame.get("hwnd")) is not int or frame["hwnd"] <= 0):
            raise EditorQAError("EDITOR_PROTOCOL_INVALID")
        self.hwnd = frame["hwnd"]
        self.owner = {"pid": self.process.pid, "started": self.native.started(self.process._handle),
                      "hwnd": self.hwnd, "nonce": self.nonce}
        # One initial activation of our exact spawned HWND is permitted. A
        # later user switch is sticky Stop, never a reason to steal focus back.
        if self.native.hwnd_pid(self.hwnd) != self.process.pid: raise EditorQAError("EDITOR_IDENTITY_CHANGED")
        self.native.user.SetForegroundWindow(self.hwnd)
        self.guard()
        def monitor_focus():
            while not self.monitor_finished.wait(.02):
                if self.closing: return
                try: self.guard()
                except EditorQAError: return
        self.focus_monitor = threading.Thread(target=monitor_focus, daemon=True, name="owned-editor-focus-guard")
        self.focus_monitor.start()
        self.window = self.request_sync("bind")["window"]
        return self

    def request_sync(self, operation, **arguments):
        from desktop_pet.agents.service import _ProcessTree
        from agent_backend.nanobot.desktop_controls import DesktopControlError, MAX_OUTPUT
        if operation not in {"bind", "snapshot", "read", "act"}: raise EditorQAError("EDITOR_OPERATION_DENIED")
        self.guard()
        request = {"operation": operation, "owner": self.owner, **arguments}
        if self.window is not None:
            supplied = arguments.get("window")
            if supplied is not None and supplied != self.window: raise EditorQAError("EDITOR_IDENTITY_CHANGED")
            request["window"] = self.window
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(payload) > 16384: raise EditorQAError("EDITOR_PROTOCOL_INVALID")
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            process = subprocess.Popen([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-MTA",
                "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "tools/qa_owned_editor_uia.ps1")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=0x08000000 | 0x00000200, env={key: value for key, value in os.environ.items()
                    if not re.search("API.?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT", key, re.I)})
        except Exception:
            self.helper_launch_unknown = True
            self.stop("EDITOR_HELPER_CLEANUP_UNCONFIRMED")
            raise EditorQAError(self.reason) from None
        record = [process, None]; self.helpers.append(record)
        try:
            handle = self.lifecycle.register(process, "owned-editor-helper-" + uuid4().hex, None)
            record[1] = handle
            self.lifecycle.attach_tree(handle, _ProcessTree(process))
        except Exception:
            self.helper_launch_unknown = True
            self.stop("EDITOR_HELPER_CLEANUP_UNCONFIRMED")
            raise EditorQAError(self.reason) from None
        finished = threading.Event()
        def watch():
            while not finished.wait(.02):
                try: self.guard()
                except EditorQAError:
                    self.lifecycle.request_stop(handle); return
        watcher = threading.Thread(target=watch, daemon=True, name="owned-editor-action-guard")
        watcher.start()
        closed = False
        try:
            self.guard(); self.lifecycle.mark_dispatched(handle)
            timeout = self.act_timeout if operation == "act" and self.act_timeout is not None else 12
            try: output, _ = process.communicate(payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                self.stop("EDITOR_NATIVE_TIMEOUT"); self.lifecycle.request_stop(handle)
                try: process.communicate(timeout=2)
                except subprocess.TimeoutExpired: pass
                raise EditorQAError("EDITOR_NATIVE_TIMEOUT") from None
            self.guard()
            if process.returncode != 0 or len(output) > MAX_OUTPUT: raise EditorQAError("EDITOR_PROTOCOL_INVALID")
            try: result = json.loads(output.decode("utf-8-sig"))
            except Exception: raise EditorQAError("EDITOR_PROTOCOL_INVALID") from None
            if type(result) is not dict: raise EditorQAError("EDITOR_PROTOCOL_INVALID")
            if result.get("error"): raise DesktopControlError(result["error"])
            if self.window is not None and result.get("window") != self.window:
                raise EditorQAError("EDITOR_IDENTITY_CHANGED")
            self.operations.append({"operation": operation, "dispatched": result.get("dispatched") is True})
            return result
        finally:
            finished.set(); watcher.join(timeout=2)
            receipt = self.lifecycle.settle(handle, timeout_seconds=2)
            closed = receipt is not None and self.lifecycle.validates(handle, receipt) and not watcher.is_alive()
            if closed:
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None: pipe.close()
            else: self.stop("EDITOR_HELPER_CLEANUP_UNCONFIRMED")
            if not closed: raise EditorQAError("EDITOR_HELPER_CLEANUP_UNCONFIRMED")

    async def request(self, operation, **arguments):
        # The standalone QA coordinator has no GUI event loop to block. Its
        # independent guard thread stops the exact helper on Esc/foreground loss.
        return self.request_sync(operation, **arguments)

    def close(self):
        self.closing = True
        self.monitor_finished.set()
        if self.process is not None and self.process.poll() is None:
            try: self.send({"type": "stop"})
            except (OSError, BrokenPipeError): pass
        closed = not (self.launch_unknown or self.helper_launch_unknown)
        if self.focus_monitor is not None:
            self.focus_monitor.join(timeout=2); closed &= not self.focus_monitor.is_alive()
        for process, handle in self.helpers:
            if handle is None:
                handle = self.lifecycle.register(process, "owned-editor-helper-cleanup-" + uuid4().hex, None)
            receipt = self.lifecycle.settle(handle, timeout_seconds=2)
            closed &= receipt is not None and self.lifecycle.validates(handle, receipt)
        if self.process is not None and self.handle is None:
            # Exact returned object, setup never dispatched: cleanup only.
            self.handle = self.lifecycle.register(self.process, "owned-editor-cleanup-" + self.nonce, None)
        if self.handle is not None:
            receipt = self.lifecycle.settle(self.handle, timeout_seconds=3)
            closed &= receipt is not None and self.lifecycle.validates(self.handle, receipt)
        if self.reader is not None:
            self.reader.join(timeout=2); closed &= not self.reader.is_alive()
        if closed:
            if self.process is not None:
                for pipe in (self.process.stdin, self.process.stdout):
                    if pipe is not None: pipe.close()
            _OWNERS.discard(self)
        self.focus_result = self.native.restore(self.previous, {self.hwnd, *self.decoys}, self.user_changed)
        return bool(closed)


def _read_owned_document(workspace):
    """Fixed leaf, stable regular file, no directory redirection or hardlinks."""
    path = Path(workspace) / "report.txt"
    parent = Path(workspace).absolute()
    directory = parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or parent.is_symlink()
            or getattr(directory, "st_file_attributes", 0) & 0x400):
        raise OSError("EDITOR_WORKSPACE_CHANGED")
    # Resolve the file against the original lexical host directory. If
    # that directory or an ancestor becomes a junction, resolving both
    # sides would incorrectly authorize the redirected external target.
    if path.resolve().parent != parent: raise OSError("EDITOR_FILE_SCOPE_INVALID")
    before = path.lstat()
    regular = (stat.S_ISREG(before.st_mode) and not path.is_symlink() and before.st_nlink == 1
               and not (getattr(before, "st_file_attributes", 0) & 0x400))
    if not regular: raise OSError("EDITOR_FILE_SCOPE_INVALID")
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if identity(opened) != identity(before): raise OSError("EDITOR_FILE_CHANGED")
        raw = stream.read(65537)
        if len(raw) > 65536 or identity(os.fstat(stream.fileno())) != identity(opened):
            raise OSError("EDITOR_FILE_CHANGED")
    if identity(path.lstat()) != identity(before): raise OSError("EDITOR_FILE_CHANGED")
    if identity(parent.lstat()) != identity(directory): raise OSError("EDITOR_WORKSPACE_CHANGED")
    return raw


def roundtrip_oracle(workspace, expected, *, first_runtime, reopened_runtime, closed_without_editor,
                     current_value, status, saved_sha, current_identity_matches, cancelled):
    """Read actual disk bytes; labels/dispatch or fixture counters never suffice."""
    try:
        raw = _read_owned_document(workspace)
        text = raw.decode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        regular = True
    except (OSError, UnicodeError): raw, text, digest, regular = b"", None, None, False
    checks = {"actual_utf8_file_matches_goal": text == expected and raw == expected.encode("utf-8"),
        "owned_regular_file": regular,
        "save_version_still_current": digest is not None and digest == saved_sha,
        "document_closed_then_reopened": closed_without_editor is True and bool(first_runtime) and bool(reopened_runtime) and first_runtime != reopened_runtime,
        "fresh_reopened_ui_value_matches_disk": current_value == expected and status == "Reopened",
        "exact_owned_native_identity_current": current_identity_matches is True,
        "no_stop_or_foreground_violation": cancelled is False}
    return {"passed": all(checks.values()), "checks": checks, "file_sha256": digest,
        "file_bytes": len(raw), "scope": "one owned native editor document roundtrip only"}


async def scripted_roundtrip(editor, marker, case):
    from agent_backend.nanobot.desktop_controls import DesktopControls, DesktopControlError
    controls = DesktopControls(editor.cancel, backend=editor, max_age_seconds=90)
    started=time.monotonic(); milestones=[]
    def record(stage, **facts):
        milestones.append({"sequence":len(milestones)+1,"stage":stage,
            "elapsed_ms":round((time.monotonic()-started)*1000,3),**facts})
    def find(snapshot, name):
        matches = [row for row in snapshot["controls"] if row["name"] == name]
        if len(matches) != 1: raise EditorQAError("EDITOR_CONTROL_MISSING_OR_AMBIGUOUS")
        return matches[0]
    async def observe(): return json.loads(await controls.observe())
    async def invoke(name):
        snapshot = await observe()
        return json.loads(await controls.act(find(snapshot, name)["ref"], "invoke"))
    first = await observe(); field = find(first, "Document text")
    first_runtime = controls._refs[field["ref"]]["target"]["runtime_id"]
    record("owned_native_document_observed",current_process_matches=first["window"]["process_id"]==editor.process.pid)
    if case == "stale":
        old = field["ref"]; editor.send({"type": "replace_editor"})
        # A private raw native snapshot does not invalidate DesktopControls'
        # old reference; rejection must come from the changed actual RuntimeID.
        raw = editor.request_sync("snapshot")
        new = next(node for node in raw["nodes"] if node["name"] == "Document text")
        try: await controls.act(old, "set_value", marker); blocked = False
        except (DesktopControlError, EditorQAError): blocked = True
        return {"passed": blocked and new["runtime_id"] != first_runtime and not (editor.workspace / "report.txt").exists(),
            "guard_suite": True, "native_goal_passed": False, "scope": "stale native RuntimeID rejection"}
    if case == "foreground":
        editor.send({"type": "show_decoy"})
        value = editor.frames.get(timeout=5)
        if value.get("type") != "editor_decoy": raise EditorQAError("EDITOR_PROTOCOL_INVALID")
        editor.native.user.SetForegroundWindow(value["hwnd"])
        try: await controls.act(field["ref"], "set_value", marker); blocked = False
        except (DesktopControlError, EditorQAError): blocked = True
        return {"passed": blocked and editor.reason == "EDITOR_FOREGROUND_MISMATCH" and not (editor.workspace / "report.txt").exists(),
            "guard_suite": True, "native_goal_passed": False, "scope": "own same-PID decoy HWND foreground rejection"}
    if case == "cancel":
        editor.stop("EDITOR_CONTROLLED_CANCEL")
        before = len(editor.operations)
        try: await controls.act(field["ref"], "set_value", marker); blocked = False
        except (DesktopControlError, EditorQAError): blocked = True
        return {"passed": blocked and before == len(editor.operations) and not (editor.workspace / "report.txt").exists(),
            "guard_suite": True, "native_goal_passed": False, "scope": "cancel before native effect"}
    value = json.loads(await controls.act(field["ref"], "set_value", marker))
    if value["postcondition"]["passed"] is not True: raise EditorQAError("EDITOR_NATIVE_VALUE_NOT_READ_BACK")
    record("native_value_read_back",passed=True)
    if case == "timeout": editor.act_timeout = .2
    try: await invoke("Save document")
    except (EditorQAError, DesktopControlError):
        if case != "timeout": raise
        return {"passed": editor.reason == "EDITOR_NATIVE_TIMEOUT" and not (editor.workspace / "report.txt").exists(),
            "guard_suite": True, "native_goal_passed": False, "scope": "bounded native helper timeout without replay"}
    try: saved_sha = hashlib.sha256(_read_owned_document(editor.workspace)).hexdigest()
    except OSError: saved_sha = None
    record("independent_saved_file_observed",file_sha256=saved_sha)
    await invoke("Close document")
    closed = await observe()
    closed_without_editor = (not any(row["name"] == "Document text" for row in closed["controls"])
        and find(closed, "Document status")["state"].get("value") == "Closed")
    record("document_closed_observed",editor_absent_and_closed_status=closed_without_editor)
    await invoke("Reopen document")
    final = await observe()
    reopened = [row for row in final["controls"] if row["name"] == "Document text"]
    node = reopened[0] if len(reopened) == 1 else None
    record("document_reopened_observed",fresh_control_identity=node is not None and
        controls._refs[node["ref"]]["target"]["runtime_id"] != first_runtime,
        value_sha256=hashlib.sha256(node["state"].get("value","").encode("utf-8")).hexdigest() if node else None)
    result = roundtrip_oracle(editor.workspace, marker, first_runtime=first_runtime,
        reopened_runtime=controls._refs[node["ref"]]["target"]["runtime_id"] if node else None,
        closed_without_editor=closed_without_editor, current_value=node["state"].get("value") if node else None,
        status=find(final, "Document status")["state"].get("value"), saved_sha=saved_sha,
        current_identity_matches=final["window"]["process_id"] == editor.process.pid,
        cancelled=editor.cancel.is_set())
    expected_pass = case == "positive"
    return {"passed": result["passed"] is expected_pass, "native_goal_passed": result["passed"],
        "guard_suite": not expected_pass, "oracle": result,"milestones":milestones}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--case", choices=("positive", "save_noop", "save_wrong", "reopen_noop", "stale", "foreground", "cancel", "timeout"), default="positive")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/owned-editor-native/verification.json")
    args = parser.parse_args()
    if not args.native:
        print(json.dumps({"status": "not_run", "native_input": False, "model_called": False})); return 0
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()): raise ValueError("EDITOR_OUTPUT_SCOPE_INVALID")
    parent = ROOT / "agent_workspace"; parent.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="owned-editor-", dir=parent)
    editor = None; report = {"passed": False, "case": args.case}
    started = time.monotonic()
    try:
        fault = args.case if args.case in {"save_noop", "save_wrong", "reopen_noop"} else "none"
        editor = OwnedEditor(temporary.name, fault=fault)
        editor.start()
        report.update(asyncio.run(scripted_roundtrip(editor, "AURA 编辑器 ✅ " + uuid4().hex[:12], args.case)))
    except Exception as error:
        code = getattr(error, "code", None)
        report["error_code"] = code if isinstance(code, str) and re.fullmatch("[A-Z_]{1,80}", code) else "EDITOR_QA_FAILED"
    finally:
        try: closed = editor.close() if editor is not None else True
        except Exception: closed = False
        if "scope" in report: report["case_scope"] = report["scope"]
        report.update(native_windows=os.name == "nt", model_called=False, scripted_native=True,
            user_applications_operated=False, clipboard_used=False, screenshot_files_written=0,
            exact_fixture_and_helpers_closed=closed, duration_seconds=round(time.monotonic()-started, 3),
            focus_result=getattr(editor, "focus_result", "unavailable"),
            scope="owned native Qt document save/close/reopen; not model ability or general app control")
        report["native_operations"] = getattr(editor,"operations",[])
        report["passed"] = report["passed"] and closed
        if closed: temporary.cleanup()
        else:
            # Retention must also survive the host interpreter's atexit
            # finalizer pass; an unknown owner is not permission to delete.
            temporary._finalizer.detach()
            _OWNERS.add((editor, temporary))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True)); return 0 if report["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
