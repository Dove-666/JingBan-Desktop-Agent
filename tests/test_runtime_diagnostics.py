"""Local diagnostic whitelist/ownership tests; no QApplication or network."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from desktop_pet import runtime_diagnostics as module
from desktop_pet.runtime_diagnostics import RuntimeDiagnostics


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix="aura-runtime-diagnostic-test-")
        self.directory = Path(self.owner.name)
        self.log = RuntimeDiagnostics(self.directory, "2026.11.05-r20")

    def tearDown(self):
        self.log.close()
        self.owner.cleanup()

    def records(self):
        return [json.loads(line) for file in self.log.directory.glob("runtime*.jsonl")
                for line in file.read_text(encoding="ascii").splitlines()]

    def test_only_whitelisted_metadata_and_lexical_internal_location(self):
        secret = "SECRET-CANARY-CHAT-KEY-MESSAGE-NOTES"
        filename = str(Path(module.__file__).parent / "app.py")
        try:
            exec(compile("raise RuntimeError('" + secret + "')", filename, "exec"))
        except RuntimeError as error:
            error.add_note(secret) if hasattr(error, "add_note") else None
            with patch.object(Path, "resolve", side_effect=AssertionError("traceback filesystem access")):
                self.log.record("exception", code="MAIN_UNCAUGHT", stage="event_loop", error=error)
        record = self.records()[0]
        self.assertEqual(record["exception_type"], "RuntimeError")
        self.assertEqual(record["location"], {"module": "desktop_pet/app.py", "line": 1})
        self.assertNotIn(secret, self.log.path.read_text())
        self.assertNotIn(str(self.directory), self.log.path.read_text())
        self.assertTrue(set(record) <= {"time", "pid", "run", "revision", "event", "code", "stage", "source", "exit_code", "exception_type", "location"})

    def test_unc_traceback_and_unknown_type_do_not_leak_or_touch_network_paths(self):
        secret_type = type("SECRET_PRIVATE_TYPE", (Exception,), {})
        try:
            exec(compile("raise secret_type('SECRET_MESSAGE')", "\\\\private-server\\SECRET\\x.py", "exec"))
        except secret_type as error:
            with patch.object(Path, "resolve", side_effect=AssertionError("UNC resolution")):
                self.log.record("exception", code="MAIN_UNCAUGHT", error=error)
        record = self.records()[0]
        self.assertEqual(record["exception_type"], "OtherException")
        self.assertNotIn("location", record)
        self.assertNotIn("SECRET", self.log.path.read_text())

    def test_rotation_is_two_bounded_files_and_every_record_is_valid_json(self):
        with patch.object(module, "MAX_LOG_BYTES", 1024):
            for _ in range(40): self.log.record("startup")
        files = list(self.log.directory.glob("runtime*.jsonl"))
        self.assertEqual(len(files), 2)
        for file in files:
            self.assertLessEqual(file.stat().st_size, 1024)
            for line in file.read_bytes().splitlines():
                self.assertLessEqual(len(line) + 1, module.MAX_RECORD_BYTES)
                self.assertEqual(json.loads(line)["event"], "startup")

    def test_io_failure_is_open_and_recursive_hook_chains_only_once(self):
        calls = []
        def original(kind, error, tb):
            calls.append(kind)
            sys.excepthook(kind, error, tb)
        with patch.object(sys, "excepthook", original), patch.object(threading, "excepthook", lambda args: None):
            self.log.install_hooks()
            with patch.object(module.os, "open", side_effect=OSError("SECRET_IO_MESSAGE")):
                sys.excepthook(RuntimeError, RuntimeError("SECRET"), None)
            self.log.close()
            self.assertIs(sys.excepthook, original)
        self.assertEqual(calls, [RuntimeError])

    def test_thread_hook_and_newer_hook_ownership_are_preserved(self):
        previous_sys, previous_thread = lambda *_: None, lambda *_: None
        newer_sys, newer_thread = lambda *_: None, lambda *_: None
        with patch.object(sys, "excepthook", previous_sys), patch.object(threading, "excepthook", previous_thread):
            self.log.install_hooks()
            threading.excepthook(SimpleNamespace(exc_type=ValueError, exc_value=ValueError("SECRET"), exc_traceback=None))
            sys.excepthook, threading.excepthook = newer_sys, newer_thread
            self.log.close()
            self.assertIs(sys.excepthook, newer_sys)
            self.assertIs(threading.excepthook, newer_thread)
        self.assertEqual(self.records()[0]["code"], "THREAD_UNCAUGHT")

    def test_constructing_logger_does_not_install_global_hooks(self):
        original = sys.excepthook, threading.excepthook
        self.log.record("not_whitelisted", code="SECRET")
        self.assertEqual(original, (sys.excepthook, threading.excepthook))
        self.assertFalse(self.log.directory.exists())

    def test_hardlinks_and_oversize_unknown_files_are_preserved(self):
        for name, size in (("runtime.jsonl", 27), ("runtime.jsonl", module.MAX_LOG_BYTES + 17),
                           ("runtime.1.jsonl", module.MAX_LOG_BYTES + 17)):
            with self.subTest(name=name, size=size), tempfile.TemporaryDirectory(dir=self.directory) as owned:
                profile = Path(owned) / "profile"; profile.mkdir()
                log = RuntimeDiagnostics(profile, "2026.11.05-r20")
                log.directory.mkdir()
                canary = Path(owned) / "owned-canary"
                original = b"X" * size; canary.write_bytes(original)
                os.link(canary, log.directory / name)
                log.record("startup"); log.close()
                self.assertEqual(canary.read_bytes(), original)
                self.assertEqual((log.directory / name).stat().st_nlink, 2)
        self.log.directory.mkdir()
        original = b"Z" * (module.MAX_LOG_BYTES + 1)
        self.log.path.write_bytes(original)
        self.log.record("startup")
        self.assertEqual(self.log.path.read_bytes(), original)

    def test_unhashable_event_and_secret_revision_do_not_leak_or_raise(self):
        self.log.record([])
        alternate = RuntimeDiagnostics(self.directory, "sk-SYNTHETIC_SECRET_REVISION")
        alternate.record("startup"); alternate.close()
        self.assertEqual(self.records()[0]["revision"], "unknown")
        self.assertNotIn("SYNTHETIC_SECRET", self.log.path.read_text())

    def test_bad_ancestor_is_rejected_before_visiting_its_descendants(self):
        visited = []
        def safe(path, **_):
            visited.append(path)
            return path != self.directory
        target = self.directory / "child" / "diagnostics"
        with patch.object(RuntimeDiagnostics, "_safe_path", side_effect=safe):
            self.assertFalse(RuntimeDiagnostics._safe_ancestors(target))
        self.assertNotIn(self.directory / "child", visited)
        self.assertNotIn(target, visited)
