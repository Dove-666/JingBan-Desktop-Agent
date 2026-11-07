"""Bounded lifecycle metadata; never serialize exception text or user data."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading


MAX_LOG_BYTES = 128 * 1024
MAX_RECORD_BYTES = 1024
_ROOT = os.path.normcase(os.path.normpath(str(Path(__file__).parent.parent)))
_EVENTS = {"startup", "quit_signal", "about_to_quit", "event_loop_return", "exception"}
_CODES = {"MAIN_UNCAUGHT", "STARTUP_IMAGE_ERROR", "STARTUP_CONFIG_ERROR", "SYS_UNCAUGHT",
          "THREAD_UNCAUGHT", "CLEANUP_SESSION", "CLEANUP_INSTANCE", "CLEANUP_THEME"}
_STAGES = {"theme", "session", "show", "event_loop", "cleanup"}
_SOURCES = {"session_finished", "qt_about_to_quit"}
_TYPES = {"BaseException", "Exception", "RuntimeError", "ValueError", "TypeError", "OSError",
          "PermissionError", "FileNotFoundError", "ImportError", "ModuleNotFoundError", "KeyError",
          "IndexError", "AttributeError", "AssertionError", "MemoryError", "RecursionError",
          "KeyboardInterrupt", "SystemExit", "PetImageError", "SettingsError", "SingleInstanceError",
          "ApiError", "CancelledError"}


def _location(tb):
    """Lexical known-source filtering only: no stat/resolve/linecache/UNC I/O."""
    location = None
    for _ in range(256):
        if tb is None:
            break
        name = os.path.normcase(os.path.normpath(tb.tb_frame.f_code.co_filename))
        prefix = _ROOT + os.sep
        if name.startswith(prefix):
            relative = name[len(prefix):].replace("\\", "/")
            if ((relative == "main.py" or relative.startswith("desktop_pet/"))
                    and re.fullmatch(r"[A-Za-z0-9_/.-]{1,160}\.py", relative)
                    and ".." not in relative.split("/")):
                location = {"module": relative, "line": max(0, min(int(tb.tb_lineno), 1000000000))}
        tb = tb.tb_next
    return location


class RuntimeDiagnostics:
    def __init__(self, profile_directory, revision):
        self.directory = Path(profile_directory) / "diagnostics"
        self.path = self.directory / "runtime.jsonl"
        self.previous_path = self.directory / "runtime.1.jsonl"
        self.run_id = secrets.token_hex(8)
        self.revision = revision if isinstance(revision, str) and re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}-r[0-9]{1,6}", revision) else "unknown"
        self._lock = threading.Lock()
        self._local = threading.local()
        self._closed = False
        self._bindings = []
        self._previous_sys = None
        self._previous_thread = None
        self._installed = False
        self._sys_hook = self._on_sys
        self._thread_hook = self._on_thread

    @staticmethod
    def _safe_path(path, *, missing=False, directory=False):
        value = str(path)
        if not path.is_absolute() or value.startswith(("\\\\", "//")):
            return False
        try:
            info = path.lstat()
        except FileNotFoundError:
            return missing
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
        return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1

    @classmethod
    def _safe_ancestors(cls, path):
        return all(cls._safe_path(parent, directory=True) for parent in (*reversed(path.parents), path))

    def record(self, event, *, code=None, stage=None, source=None, exit_code=None, error=None, tb=None):
        if self._closed or not isinstance(event, str) or event not in _EVENTS or getattr(self._local, "writing", False):
            return
        self._local.writing = True
        try:
            record = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      "pid": os.getpid(), "run": self.run_id, "revision": self.revision, "event": event}
            if code in _CODES: record["code"] = code
            if stage in _STAGES: record["stage"] = stage
            if source in _SOURCES: record["source"] = source
            if type(exit_code) is int: record["exit_code"] = max(-2147483648, min(exit_code, 2147483647))
            if error is not None:
                name = type(error).__name__
                record["exception_type"] = name if name in _TYPES else "OtherException"
                location = _location(tb if tb is not None else error.__traceback__)
                if location: record["location"] = location
            payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(payload) > MAX_RECORD_BYTES:
                record.pop("location", None)
                payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(payload) > MAX_RECORD_BYTES: return
            with self._lock:
                if not self._safe_ancestors(self.directory.parent): return
                if not self._safe_path(self.directory, missing=True, directory=True): return
                self.directory.mkdir(exist_ok=True)
                if not self._safe_ancestors(self.directory): return
                for path in (self.path, self.previous_path):
                    if not self._safe_path(path, missing=True): return
                    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                        return  # Do not truncate an existing unknown/oversized file.
                size = self.path.stat().st_size if self.path.exists() else 0
                if size + len(payload) > MAX_LOG_BYTES:
                    os.replace(self.path, self.previous_path)
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                try:
                    info = os.fstat(descriptor)
                    current = self.path.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                            or not self._safe_path(self.path)
                            or info.st_size + len(payload) > MAX_LOG_BYTES): return
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
        except Exception:
            pass  # Diagnostics must never prevent launch, shutdown, or cleanup.
        finally:
            self._local.writing = False

    def bind(self, signal, event, **fields):
        def received(*_): self.record(event, **fields)
        try:
            signal.connect(received)
            self._bindings.append((signal, received))
        except Exception:
            pass

    def install_hooks(self):
        if self._closed or self._installed: return
        self._previous_sys, self._previous_thread = sys.excepthook, threading.excepthook
        sys.excepthook, threading.excepthook = self._sys_hook, self._thread_hook
        self._installed = True

    def _on_sys(self, kind, error, tb):
        if getattr(self._local, "hook", False): return
        self._local.hook = True
        try:
            self.record("exception", code="SYS_UNCAUGHT", error=error, tb=tb)
            if callable(self._previous_sys): self._previous_sys(kind, error, tb)
        finally:
            self._local.hook = False

    def _on_thread(self, args):
        if getattr(self._local, "hook", False): return
        self._local.hook = True
        try:
            self.record("exception", code="THREAD_UNCAUGHT", error=args.exc_value, tb=args.exc_traceback)
            if callable(self._previous_thread): self._previous_thread(args)
        finally:
            self._local.hook = False

    def close(self):
        self._closed = True
        for signal, callback in self._bindings:
            try: signal.disconnect(callback)
            except Exception: pass
        self._bindings.clear()
        if self._installed and sys.excepthook is self._sys_hook:
            sys.excepthook = self._previous_sys
        if self._installed and threading.excepthook is self._thread_hook:
            threading.excepthook = self._previous_thread
