"""Stub Qt entrypoint wiring, temporary private profile, no GUI/network."""
from contextlib import ExitStack, redirect_stderr
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from desktop_pet import app as module


class Signal:
    def __init__(self): self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
    def disconnect(self, callback): self.callbacks.remove(callback)
    def emit(self):
        for callback in list(self.callbacks): callback()


class StartupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix="aura-startup-diagnostic-test-")
        self.root = Path(self.owner.name)
        self.settings = SimpleNamespace(config_path=str(self.root / "settings.ini"))
        self.instance = SimpleNamespace(activation_requested=Signal(), start=lambda *_: True, release=lambda: None)
        self.theme = SimpleNamespace(close=lambda: None)
        self.session = SimpleNamespace(finished=Signal(), shutdown=lambda: None, show=lambda: None, open_debug_panel=lambda: None)
        self.qt = SimpleNamespace(aboutToQuit=Signal(), setWindowIcon=lambda *_: None, quit=lambda: None)
        for name in ("setApplicationName", "setApplicationDisplayName", "setApplicationVersion", "setQuitOnLastWindowClosed"):
            setattr(self.qt, name, lambda *_: None)
        def loop():
            self.session.finished.emit(); self.qt.aboutToQuit.emit()
            return 7
        self.qt.exec = loop

    def tearDown(self): self.owner.cleanup()

    def run_main(self):
        config = SimpleNamespace(show_debug_panel=False, silent_start=None, autostart=False, image_path=Path("owned.png"))
        with ExitStack() as stack:
            for target, value in (("configure_webengine_rendering", lambda: None), ("parse_config", lambda *_: config),
                ("QApplication", SimpleNamespace(instance=lambda: self.qt)), ("QIcon", lambda *_: None),
                ("SingleInstance", lambda *_: self.instance), ("ThemeManager", lambda *_: self.theme),
                ("ApplicationController", lambda *_, **__: self.session)):
                stack.enter_context(patch.object(module, target, value))
            return module.main([], settings=self.settings)

    def records(self):
        path = self.root / "diagnostics/runtime.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_real_entrypoint_records_events_preserves_return_and_restores_hooks(self):
        hooks = sys.excepthook, threading.excepthook
        self.assertEqual(self.run_main(), 7)
        self.assertEqual([row["event"] for row in self.records()], ["startup", "quit_signal", "about_to_quit", "event_loop_return"])
        self.assertEqual(self.records()[-1]["exit_code"], 7)
        self.assertEqual(hooks, (sys.excepthook, threading.excepthook))
        self.assertEqual(len(self.session.finished.callbacks), 1)  # Original app.quit remains.
        self.assertEqual(len(self.qt.aboutToQuit.callbacks), 1)  # Original shutdown remains.

    def test_forwarded_instance_installs_no_diagnostic_or_hook(self):
        self.instance.start = lambda *_: False
        hooks = sys.excepthook, threading.excepthook
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.records(), [])
        self.assertEqual(hooks, (sys.excepthook, threading.excepthook))

    def test_main_exception_is_recorded_before_hook_restore_and_reraised_unchanged(self):
        error = RuntimeError("SECRET-MAIN-MESSAGE")
        def loop(): raise error
        self.qt.exec = loop
        hooks = sys.excepthook, threading.excepthook
        with self.assertRaises(RuntimeError) as result: self.run_main()
        self.assertIs(result.exception, error)
        self.assertEqual(self.records()[-1]["code"], "MAIN_UNCAUGHT")
        self.assertNotIn("SECRET", json.dumps(self.records()))
        self.assertEqual(hooks, (sys.excepthook, threading.excepthook))

    def test_cleanup_masking_order_and_original_exception_semantics_are_retained(self):
        calls = []; session_error = RuntimeError("SECRET-SESSION"); release_error = ValueError("SECRET-RELEASE")
        def shutdown(): calls.append("session"); raise session_error
        def release(): calls.append("release"); raise release_error
        self.session.shutdown = shutdown; self.instance.release = release
        self.theme.close = lambda: calls.append("theme")
        self.qt.exec = lambda: 0
        with self.assertRaises(ValueError) as result: self.run_main()
        self.assertIs(result.exception, release_error)
        self.assertEqual(calls, ["session", "release"])
        self.assertEqual([row["code"] for row in self.records() if "code" in row], ["CLEANUP_SESSION", "CLEANUP_INSTANCE"])
        self.assertNotIn("SECRET", json.dumps(self.records()))

    def test_diagnostic_disk_failure_does_not_prevent_original_startup_or_return(self):
        with patch("desktop_pet.runtime_diagnostics.os.open", side_effect=OSError("SECRET_DISK_FAILURE")):
            self.assertEqual(self.run_main(), 7)
        self.assertEqual(self.records(), [])
