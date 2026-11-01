"""Capture only dedicated QWidget and GPU QOpenGLWindow foreground probes.

No API, credentials, production settings, or full-desktop capture are used.
The observer is guarded by the synthetic child's exact HWND and PID. Each
probe is closed before starting the next, and the original focus is restored.
"""

import ctypes
from ctypes import wintypes
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from desktop_pet.perception.screen_observer import (
    ForegroundInfo, ScreenObserver, capture_foreground, windows_foreground,
)
from desktop_pet.perception.settings import PerceptionSettings
from tests.windows_smoke import NativeEnvironmentUnavailable, controlled_process_ids, native_environment


def publish_state(state, payload):
    path = Path(state)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def probe(state):
    app = QApplication([])
    widget = QWidget(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
    widget.setWindowTitle("AURA Synthetic Capture Probe")
    widget.setStyleSheet("background:#2463eb;color:white;font-size:24px;")
    widget.resize(420, 280)
    layout = QVBoxLayout(widget)
    layout.addWidget(QLabel("AURA SYNTHETIC IMAGE\nNo private desktop content"))
    widget.show()
    widget.activateWindow()
    QTimer.singleShot(200, lambda: publish_state(state, {
        "hwnd": int(widget.winId()), "pid": os.getpid(), "kind": "gdi_qwidget",
    }))
    QTimer.singleShot(15000, app.quit)
    return app.exec()


class SyntheticGpuWindow(QOpenGLWindow):
    """Draw known colors through a real GL context, without image assets."""

    def __init__(self):
        super().__init__()
        self.setTitle("AURA Synthetic GPU Capture Probe")
        self.setFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(420, 280)
        self.frames = 0
        self.gl_info = {"context_valid": False, "renderer": "", "vendor": "", "version": ""}

    def initializeGL(self):
        context = self.context()
        self.gl_info["context_valid"] = bool(context and context.isValid())
        if not self.gl_info["context_valid"]:
            return
        functions = context.functions()
        functions.initializeOpenGLFunctions()
        self.gl_info.update({
            "renderer": functions.glGetString(0x1F01) or "",  # GL_RENDERER
            "vendor": functions.glGetString(0x1F00) or "",    # GL_VENDOR
            "version": functions.glGetString(0x1F02) or "",   # GL_VERSION
        })

    def paintGL(self):
        if not self.gl_info["context_valid"]:
            return
        functions = self.context().functions()
        width = round(self.width() * self.devicePixelRatio())
        height = round(self.height() * self.devicePixelRatio())
        functions.glDisable(0x0C11)  # GL_SCISSOR_TEST
        functions.glClearColor(36 / 255, 99 / 255, 235 / 255, 1.0)
        functions.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT
        functions.glEnable(0x0C11)
        # GL's lower-left origin puts orange in the top half of the image.
        functions.glScissor(0, height // 2, width, height - height // 2)
        functions.glClearColor(244 / 255, 181 / 255, 68 / 255, 1.0)
        functions.glClear(0x00004000)
        functions.glDisable(0x0C11)
        functions.glFinish()
        self.frames += 1


def probe_gpu(state):
    app = QApplication([])
    widget = SyntheticGpuWindow()
    widget.show()
    widget.requestActivate()

    def publish():
        publish_state(state, {
            "hwnd": int(widget.winId()), "pid": os.getpid(), "kind": "gpu_qopenglwindow",
            "rendered_frames": widget.frames, **widget.gl_info,
        })

    QTimer.singleShot(500, publish)
    QTimer.singleShot(15000, app.quit)
    return app.exec()


def sample_color_fractions(image):
    """Ignore the caption/frame and inspect both colors in the inner area."""
    margin_x = max(1, image.width() // 10)
    margin_y = max(1, image.height() // 5)
    colors = [image.pixelColor(x, y)
              for y in range(margin_y, image.height() - margin_y, 6)
              for x in range(margin_x, image.width() - margin_x, 6)]
    assert colors, "Synthetic capture has no sampleable pixels"
    return {
        "blue": sum(c.blue() > 180 and c.red() < 80 and 60 < c.green() < 140
                    for c in colors) / len(colors),
        "orange": sum(c.red() > 210 and 140 < c.green() < 210 and c.blue() < 100
                      for c in colors) / len(colors),
    }


def run():
    if sys.platform != "win32":
        raise SystemExit("Requires Windows.")
    app = QApplication([])
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    previous = user.GetForegroundWindow()
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    result = {
        "status": "running", "stage": "probe_startup", "probes": {}, "native_foreground_metadata": False,
        "native_window_capture": False, "user_desktop_captured": False,
        "full_desktop_captured": False, "network_requests": 0,
        "production_configuration_read": False, "production_credentials_read": False,
        "environment": native_environment(user), "native_calls": [],
        "limitations": [
            "Synthetic windows only; no actual Douyin content was captured or uploaded.",
            "DRM, protected video, mixed-DPI monitors, and real-app accuracy are not verified.",
        ],
    }

    def native_call(name, *args):
        ctypes.set_last_error(0)
        value = getattr(user, name)(*args)
        result["native_calls"].append({"api": name, "result": int(value or 0),
                                       "success": bool(value), "last_error": ctypes.get_last_error(),
                                       "stage": result["stage"]})
        return value

    with tempfile.TemporaryDirectory(prefix="aura-capture-probe-") as directory:
        try:
            for kind, option in (("gdi_qwidget", "--probe"), ("gpu_qopenglwindow", "--probe-gpu")):
                state = Path(directory) / f"{kind}.json"
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__)), option, str(state)],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                hwnd = 0
                try:
                    deadline = time.monotonic() + 8
                    while not state.exists() and time.monotonic() < deadline:
                        assert child.poll() is None, "Synthetic probe exited before publishing its HWND"
                        app.processEvents()
                        time.sleep(0.02)
                    assert state.exists(), "Synthetic probe did not start"
                    published = json.loads(state.read_text(encoding="utf-8"))
                    hwnd = published["hwnd"]
                    result["probes"][kind] = {"status": "running"}
                    owner = wintypes.DWORD()
                    user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                    assert published["pid"] == owner.value and owner.value in controlled_process_ids(child.pid), (
                        "Synthetic probe HWND must belong to this test's explicitly created process tree"
                    )
                    assert published["pid"] != os.getpid(), "Probe must use a separate process"
                    if kind == "gpu_qopenglwindow":
                        result["probes"][kind]["gl"] = {key: published[key] for key in
                            ("context_valid", "renderer", "vendor", "version", "rendered_frames")}
                        if not published["context_valid"] or published["rendered_frames"] < 1:
                            raise NativeEnvironmentUnavailable("A painted synthetic GPU context was not available")
                    result["stage"] = kind + ":foreground_prerequisite"
                    if not native_call("SetWindowPos", hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053):
                        raise NativeEnvironmentUnavailable("Windows could not raise the synthetic probe")
                    # One activation attempt only. Do not attach to another
                    # application's input thread or repeatedly steal focus.
                    if not native_call("SetForegroundWindow", hwnd):
                        raise NativeEnvironmentUnavailable("Windows denied foreground activation of the synthetic probe")

                    def guarded_metadata():
                        foreground = user.GetForegroundWindow()
                        pid = wintypes.DWORD()
                        user.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
                        if (foreground, pid.value) != (hwnd, published["pid"]):
                            return ForegroundInfo()
                        # The production API rechecks the expected identity
                        # before reading the title, closing the TOCTOU gap.
                        info = windows_foreground(expected_hwnd=hwnd, expected_pid=published["pid"])
                        return info if (info.hwnd, info.pid) == (hwnd, published["pid"]) else ForegroundInfo()

                    focus_deadline = time.monotonic() + 1.5
                    while user.GetForegroundWindow() != hwnd and time.monotonic() < focus_deadline:
                        app.processEvents()
                        time.sleep(0.02)
                    if user.GetForegroundWindow() != hwnd:
                        raise NativeEnvironmentUnavailable("The synthetic probe did not become foreground")
                    if guarded_metadata().hwnd != hwnd:
                        raise NativeEnvironmentUnavailable("The controlled probe's foreground metadata is unavailable")
                    # Wait for composition after raising only this synthetic HWND.
                    composed_until = time.monotonic() + 0.25
                    while time.monotonic() < composed_until:
                        app.processEvents()
                        time.sleep(0.01)
                    if guarded_metadata().hwnd != hwnd:
                        raise NativeEnvironmentUnavailable("Foreground changed before the controlled capture")
                    result["stage"] = kind + ":window_capture"
                    observer = ScreenObserver(metadata_provider=guarded_metadata, capture=capture_foreground)
                    observed = observer.observe(replace(PerceptionSettings(), enabled=True))
                    if observed.blocked_code in {"context_changed", "privacy", "own_window"}:
                        raise NativeEnvironmentUnavailable("Controlled foreground metadata changed or was unavailable during capture")
                    assert not observed.blocked_reason, observed.blocked_reason
                    assert observed.image is not None and not observed.image.isNull()
                    assert observed.info.hwnd == hwnd and observed.info.pid == published["pid"]
                    image = observed.image
                    fractions = sample_color_fractions(image)
                    if kind == "gdi_qwidget":
                        assert fractions["blue"] > 0.65, "Capture did not contain the blue QWidget probe"
                    else:
                        assert fractions["blue"] > 0.25 and fractions["orange"] > 0.25, (
                            "Capture did not contain both rendered GL colors"
                        )
                    probe_result = {
                        "status": "passed", "foreground_guard": True,
                        "preview_size": [image.width(), image.height()],
                        "color_fractions": {key: round(value, 3) for key, value in fractions.items()},
                    }
                    if kind == "gpu_qopenglwindow":
                        probe_result["gl"] = {key: published[key] for key in
                                              ("context_valid", "renderer", "vendor", "version", "rendered_frames")}
                    result["probes"][kind] = probe_result
                    image.save(str(artifacts / f"native_perception_{kind}.png"))
                    if kind == "gdi_qwidget":
                        # Preserve the existing report/image for callers of this smoke test.
                        image.save(str(artifacts / "native_perception_probe.png"))
                        result.update({"synthetic_blue_ratio": round(fractions["blue"], 3),
                                       "preview_size": probe_result["preview_size"]})
                finally:
                    if result["probes"].get(kind, {}).get("status") == "running":
                        result["probes"][kind]["status"] = "incomplete" if isinstance(sys.exc_info()[1], NativeEnvironmentUnavailable) else "failed"
                    if hwnd:
                        # Close only a HWND still owned by our explicit child.
                        owner = wintypes.DWORD()
                        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                        if owner.value in controlled_process_ids(child.pid):
                            native_call("PostMessageW", hwnd, 0x0010, 0, 0)
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # The venv launcher may own a real interpreter child: terminate
                        # only this explicitly created process tree if close is stuck.
                        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=5, check=False)
                        child.wait(timeout=5)
                    child.communicate(timeout=1)
            result.update({"status": "passed", "stage": "completed", "native_foreground_metadata": True,
                           "native_window_capture": True})
            print(json.dumps(result))
            return 0
        except NativeEnvironmentUnavailable as error:
            result.update({"status": "incomplete", "reason": str(error)})
            print(json.dumps(result))
            return 2
        except Exception as error:
            result.update({"status": "failed", "error": str(error)})
            print(json.dumps(result))
            return 1
        finally:
            if previous:
                # Restore the original focus once without inspecting its title.
                result["focus_restore_requested"] = bool(native_call("SetForegroundWindow", previous))
            (artifacts / "native_perception_smoke.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        raise SystemExit(probe(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == "--probe-gpu":
        raise SystemExit(probe_gpu(sys.argv[2]))
    raise SystemExit(run())
