"""Render the integrated desktop controls with temporary, isolated settings."""

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.settings_manager import SettingsManager, CredentialStore
from tests.test_remote_controller import spin_until
from tests.test_settings import FakeVault


def working_set_bytes():
    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
    if not query(current(), ctypes.byref(counters), counters.cb):
        raise RuntimeError("Cannot inspect isolated process memory")
    return counters.WorkingSetSize


def settle(seconds=.4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
        time.sleep(.003)


def main():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    directory = tempfile.TemporaryDirectory(prefix="aura-remote-native-")
    settings = SettingsManager(QSettings(str(Path(directory.name) / "settings.ini"), QSettings.Format.IniFormat),
                               CredentialStore(backend=FakeVault()))
    session = ApplicationController(PetConfig(), settings=settings)
    output = ROOT / "artifacts" / "remote_connection"
    output.mkdir(parents=True, exist_ok=True)
    transport = None
    try:
        session.show()
        session.open_remote_settings()
        panel = session.debug_panel.remote_panel
        assert session.debug_panel.tabs.currentWidget() is panel
        assert not session.remote.enabled and not panel.enabled_checkbox.isChecked()
        settle()
        before = working_set_bytes()
        panel.enabled_checkbox.click()
        spin_until(lambda: session.remote.status["state"] in ("listening", "error"), 40)
        assert session.remote.status["state"] == "listening", "isolated encrypted endpoint did not start"
        transport = session.remote._transport
        panel.pair_button.click()
        assert panel.pair_code.text()
        panel.cancel_pair_button.click()
        assert not panel.pair_code.text() and session.remote.pairing == {}
        settle()
        after = working_set_bytes()
        assert session.debug_panel.grab().save(str(output / "desktop_remote_panel.png"))
        panel.enabled_checkbox.click()
        assert not session.remote.enabled
        spin_until(lambda: not transport._thread.is_alive(), 10)
        report = {"passed": True, "configuration_isolated": True,
                  "production_credentials_accessed": False, "native_window_rendered": True,
                  "checks": ["tray_and_console_entry", "disabled_by_default", "start_real_endpoint",
                             "create_pair_code", "cancel_clears_code", "disable_stops_transport"],
                  "working_set_disabled_bytes": before, "working_set_listening_bytes": after,
                  "observed_increment_bytes": after - before,
                  "memory_note": "One isolated desktop run; Windows working set includes Qt/UI caches. Not a memory guarantee."}
        (output / "desktop_smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "observed_increment_mib": round((after - before) / 1024**2, 2)}))
    finally:
        session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        if transport is not None:
            spin_until(lambda: not transport._thread.is_alive(), 10)
        for window in (session.debug_panel, session.chat_window, session.pet):
            if window is not None:
                window.deleteLater()
        session.deleteLater()
        app.processEvents()
        directory.cleanup()


if __name__ == "__main__":
    main()
