"""Opt-in real Windows input test. Temporarily moves the mouse; no extra packages.

Run: .venv\Scripts\python.exe tests\windows_smoke.py
Uses a separate background process to test actual transparent click-through.
The real entrypoint uses temporary settings and memory-only credentials.
Exit 2 means native desktop/input prerequisites prevented verification.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget

from desktop_pet.config import PetConfig
from desktop_pet.animation_controller import AnimationState
from desktop_pet.pet_window import PetWindow
from desktop_pet.windows_work_area import work_area
from tests.missing_image_smoke import read_dialog_elements


class NativeEnvironmentUnavailable(RuntimeError):
    """The interactive input prerequisite failed before product verification."""


def controlled_process_ids(root_pid):
    """Include only a process created by this test and its launcher children."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == wintypes.HANDLE(-1).value:
        raise NativeEnvironmentUnavailable("Cannot identify the controlled entrypoint process tree")
    parents = {}
    entry = ProcessEntry(dwSize=ctypes.sizeof(ProcessEntry))
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {int(root_pid)}
    while True:
        added = {pid for pid, parent in parents.items() if parent in owned} - owned
        if not added:
            return owned
        owned.update(added)


def native_environment(user32):
    """Read desktop/session metadata; never inspect another window's title."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    kernel.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    def name(handle):
        value = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if handle and user32.GetUserObjectInformationW(handle, 2, value, ctypes.sizeof(value), ctypes.byref(needed)):
            return value.value
        return None
    ctypes.set_last_error(0)
    active = user32.OpenInputDesktop(0, False, 1)
    error = ctypes.get_last_error()
    try:
        session = wintypes.DWORD()
        kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
        return {"process_session_id": session.value,
                "console_session_id": kernel.WTSGetActiveConsoleSessionId(),
                "thread_desktop": name(user32.GetThreadDesktop(kernel.GetCurrentThreadId())),
                "input_desktop": name(active), "open_input_desktop_error": error,
                "remote_session": bool(user32.GetSystemMetrics(0x1000))}
    finally:
        if active:
            user32.CloseDesktop(active)


def invoke_accessible_menu_action(hwnd: int, label: str) -> dict:
    """Invoke the real QAction exposed by this process-owned popup menu.

    A desktop overlay may intercept a pointer click during process teardown.
    UI Automation targets the menu item's action without synthesizing WM_CLOSE.
    The script inspects only the already identified application-owned menu.
    """
    script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]HWND_VALUE)
$condition = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::NameProperty, 'LABEL_VALUE')
$element = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
if ($null -eq $element) { throw 'The requested application menu item is unavailable.' }
$name = $element.Current.Name
$controlType = $element.Current.ControlType.ProgrammaticName
$pattern = $null
if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
    ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
    $method = 'Windows UI Automation InvokePattern'
} elseif ($element.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$pattern)) {
    ([System.Windows.Automation.LegacyIAccessiblePattern]$pattern).DoDefaultAction()
    $method = 'Windows UI Automation LegacyIAccessible default action'
} else {
    throw 'The requested application menu item does not expose an accessible action.'
}
[Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @{
    name = $name; control_type = $controlType; method = $method
}))
""".replace("HWND_VALUE", str(hwnd)).replace("LABEL_VALUE", label.replace("'", "''"))
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        capture_output=True, encoding="utf-8", errors="replace", timeout=12,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode:
        raise AssertionError("Native menu accessible action failed: " + completed.stderr.strip())
    try:
        result = json.loads(completed.stdout.strip().lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise AssertionError("Invalid menu-action accessibility response") from exc
    assert result["name"] == label
    return result


def run_probe(state_path: Path) -> int:
    app = QApplication([])

    class Probe(QWidget):
        def __init__(self):
            # Keep the temporary probe reachable even if the current desktop
            # app uses its own topmost window. The pet is shown above it later.
            super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
            self.count = 0
            self.setWindowTitle("AURA validation background")
            self.setGeometry(QGuiApplication.primaryScreen().availableGeometry())
            self.setStyleSheet("background: #243348; color: white;")
            label = QLabel("AURA 桌宠 · 本机交互验证（结束后自动关闭）", self)
            label.setGeometry(32, 16, 600, 42)
            self.edit = QLineEdit(self)
            self.edit.setGeometry(32, 72, 260, 36)
            self.edit.setPlaceholderText("验证桌宠不会抢走键盘焦点")
            self.edit.textChanged.connect(lambda: self.publish())

        def publish(self):
            state = {"ready": True, "clicks": self.count, "text": self.edit.text(),
                     "hwnd": int(self.winId()), "pid": os.getpid(), "dpr": self.devicePixelRatioF()}
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(state_path)

        def mousePressEvent(self, event):
            self.count += 1
            self.publish()
            super().mousePressEvent(event)

    probe = Probe()
    probe.show()
    probe.raise_()
    probe.activateWindow()
    QTimer.singleShot(200, probe.publish)
    QTimer.singleShot(45000, app.quit)
    return app.exec()


def run_validation(*, bubble_only=False) -> int:
    if sys.platform != "win32":
        raise SystemExit("This test requires an interactive Windows desktop.")
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KeyboardInput(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class InputValue(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]

    class Input(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("type", wintypes.DWORD), ("value", InputValue)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    original_cursor = wintypes.POINT()
    ctypes.set_last_error(0)
    cursor_available = bool(user32.GetCursorPos(ctypes.byref(original_cursor)))
    cursor_error = ctypes.get_last_error()
    results = {"status": "running", "stage": "input_prerequisites", "bubble_only": bubble_only,
               "environment": native_environment(user32), "native_calls": [],
               "production_configuration_read": False, "production_credentials_read": False,
               "user_desktop_captured": False}
    pet = None
    normal_app = None
    probe_process = None
    allowed_input_pids = {os.getpid()}
    held_mouse_buttons = set()
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)

    def native_call(name, *args):
        ctypes.set_last_error(0)
        value = getattr(user32, name)(*args)
        results["native_calls"].append({"api": name, "result": int(value or 0), "success": bool(value),
                                         "last_error": ctypes.get_last_error(), "stage": results["stage"]})
        return value

    def wait_for(predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            QTest.qWait(25)
        raise AssertionError("Timed out waiting for native window/input response")

    def native_point(hwnd, local, dpr):
        rect = wintypes.RECT()
        assert user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return (rect.left + round(local.x() * dpr), rect.top + round(local.y() * dpr))

    def pointer(x, y, flag=None):
        if not native_call("SetCursorPos", x, y):
            raise NativeEnvironmentUnavailable("SetCursorPos failed; native pointer verification is incomplete")
        reached = wintypes.POINT()
        if not native_call("GetCursorPos", ctypes.byref(reached)) or (reached.x, reached.y) != (x, y):
            raise NativeEnvironmentUnavailable("The input desktop did not accept the test pointer position")
        if flag is not None:
            if flag in (0x0002, 0x0008):
                owner = wintypes.DWORD()
                hit = user32.WindowFromPoint(wintypes.POINT(x, y))
                user32.GetWindowThreadProcessId(hit, ctypes.byref(owner))
                if owner.value not in allowed_input_pids:
                    raise NativeEnvironmentUnavailable("A non-test window covers the intended test input target")
            event = Input(type=0, mi=MouseInput(dwFlags=flag))
            if native_call("SendInput", 1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
                raise NativeEnvironmentUnavailable("SendInput did not inject the requested test mouse event")
            if flag in (0x0002, 0x0008):
                held_mouse_buttons.add(flag)
            else:
                held_mouse_buttons.discard({0x0004: 0x0002, 0x0010: 0x0008}.get(flag))
        QTest.qWait(70)

    def type_key(key):
        foreground = user32.GetForegroundWindow()
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
        if owner.value not in allowed_input_pids:
            raise NativeEnvironmentUnavailable("Keyboard focus moved outside the controlled test processes")
        events = (Input * 2)(Input(type=1, ki=KeyboardInput(wVk=key)),
                             Input(type=1, ki=KeyboardInput(wVk=key, dwFlags=0x0002)))
        sent = native_call("SendInput", 2, events, ctypes.sizeof(Input))
        if sent != 2:
            if sent == 1:
                release = Input(type=1, ki=KeyboardInput(wVk=key, dwFlags=0x0002))
                native_call("SendInput", 1, ctypes.byref(release), ctypes.sizeof(Input))
            raise NativeEnvironmentUnavailable("SendInput did not inject both test keyboard events")

    def activate_test_window(hwnd):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value not in allowed_input_pids:
            raise NativeEnvironmentUnavailable("Refusing to activate a non-test window")
        if native_call("SetForegroundWindow", hwnd):
            return True
        # Windows may deny foreground activation from a background test runner.
        # Borrow its input queue only for this activation, then detach at once.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        current = kernel.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        attached = (foreground_thread != current and foreground_thread and
                    native_call("AttachThreadInput", current, foreground_thread, True))
        try:
            native_call("BringWindowToTop", hwnd)
            return bool(native_call("SetForegroundWindow", hwnd) or user32.GetForegroundWindow() == hwnd)
        finally:
            if attached:
                native_call("AttachThreadInput", current, foreground_thread, False)

    def click(point, right=False):
        pointer(*point, 0x0008 if right else 0x0002)
        pointer(*point, 0x0010 if right else 0x0004)

    try:
        if not cursor_available:
            results["get_cursor_error"] = cursor_error
            raise NativeEnvironmentUnavailable("GetCursorPos failed; native pointer verification is incomplete")
        with tempfile.TemporaryDirectory(prefix="aura_native_test_") as temporary:
            state_path = Path(temporary) / "probe.json"
            probe_process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--probe", str(state_path)],
                cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW,
            )

            def state():
                try:
                    return json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return {}

            wait_for(lambda: state().get("ready"))
            probe = state()
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(probe["hwnd"], ctypes.byref(owner))
            if probe["pid"] != owner.value or owner.value not in controlled_process_ids(probe_process.pid):
                raise AssertionError("Probe HWND does not belong to the explicitly created process tree")
            allowed_input_pids.add(owner.value)
            if not native_call("SetWindowPos", probe["hwnd"], wintypes.HWND(-1), 0, 0, 0, 0, 0x0053):
                raise NativeEnvironmentUnavailable("Could not raise the controlled probe")
            if not activate_test_window(probe["hwnd"]):
                raise NativeEnvironmentUnavailable("Windows denied foreground activation of the controlled probe")
            click(native_point(probe["hwnd"], QPoint(70, 86), probe["dpr"]))
            try:
                wait_for(lambda: user32.GetForegroundWindow() == probe["hwnd"])
            except AssertionError:
                foreground = user32.GetForegroundWindow()
                foreground_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
                results["foreground_diagnostic"] = {"hwnd": foreground, "pid": foreground_pid.value,
                    "controlled_probe": foreground == probe["hwnd"]}
                raise NativeEnvironmentUnavailable("Could not activate the controlled probe; input verification is incomplete") from None

            results["stage"] = "pet_and_bubble_input"
            pet = PetWindow(PetConfig(bubble_duration_ms=500))
            area = QGuiApplication.primaryScreen().availableGeometry()
            pet.move(area.topLeft() + QPoint(120, 120))
            pet._work_area_changed()
            pet.show()
            QTest.qWait(250)
            pet_hwnd = int(pet.winId())
            assert user32.GetWindowLongW(pet_hwnd, -20) & 0x00000008  # WS_EX_TOPMOST
            assert user32.GetForegroundWindow() == probe["hwnd"]
            results["topmost_without_activation"] = True
            origin = QPoint(pet.pos())
            first_pose = pet.animation.pose
            QTest.qWait(150)
            assert pet.animation.pose != first_pose and pet.pos() == origin
            results["timer_driven_idle_without_window_movement"] = True

            before = state()["clicks"]
            click(native_point(pet_hwnd, QPoint(2, 2), pet.devicePixelRatioF()))
            wait_for(lambda: state()["clicks"] == before + 1)
            results["transparent_padding_clicks_other_process"] = True

            sprite = pet.sprite_rect
            # Hold only this pixel-precision check still; other checks run animated.
            pet.refresh_timer.stop()
            # Original PNG's enclosed left hair gap, not merely window padding.
            source_hole = sprite.topLeft() + QPoint(round(185 / 1259 * sprite.width()),
                                                    round(708 / 1259 * sprite.height()))
            hole = pet.character_transform.map(QPointF(source_hole)).toPoint()
            # On 150% DPI, an edge pixel may gain a little alpha when resampled.
            # Select a fully transparent rendered pixel inside the same small hole.
            rendered = pet.grab().toImage()
            candidates = [hole + QPoint(dx, dy) for dx in range(-3, 4) for dy in range(-3, 4)]
            candidates.sort(key=lambda point: (point - hole).manhattanLength())
            hole = next(point for point in candidates
                        if not pet.character_contains(point)
                        and rendered.pixelColor(round(point.x() * pet.devicePixelRatioF()),
                                                round(point.y() * pet.devicePixelRatioF())).alpha() == 0)
            assert not pet.character_contains(hole)
            before = state()["clicks"]
            click(native_point(pet_hwnd, hole, pet.devicePixelRatioF()))
            wait_for(lambda: state()["clicks"] == before + 1)
            results["transparent_png_hole_clicks_other_process"] = True
            pet._clock.restart()
            pet.refresh_timer.start()

            center = pet.animated_sprite_rect.center().toPoint()
            assert pet.character_contains(center)
            click(native_point(pet_hwnd, center, pet.devicePixelRatioF()))
            wait_for(lambda: pet.bubble.isVisible())
            assert pet.animation.state == AnimationState.HAPPY
            results["native_click_triggers_happy"] = True
            assert user32.GetForegroundWindow() == probe["hwnd"]
            # The QLineEdit in the independent process must retain keyboard focus.
            type_key(0x41)
            wait_for(lambda: state().get("text", "").lower() == "a")
            results["click_bubble_and_background_keyboard_focus"] = True

            # A top-level speech bubble must pass native input through its
            # opaque body, including to a window in another process. Keep the
            # fixture away from the probe's QLineEdit so the click reaches the
            # probe widget and can be counted independently of keyboard focus.
            pet.move(work_area(pet.screen()).center() - pet.rect().center())
            pet.bubble.say(
                "这是桌宠评论气泡的本机交互验证。较长的中文评论应该清楚地自动换行，"
                "气泡正文也不能挡住下面的软件操作，更不能抢走正在输入的键盘焦点。",
                2500,
            )
            wait_for(lambda: pet.bubble.isVisible())
            QTest.qWait(100)
            bubble_hwnd = int(pet.bubble.winId())
            assert bubble_hwnd != pet_hwnd and pet.bubble.isWindow()
            assert user32.GetWindowLongW(bubble_hwnd, -20) & 0x00000008  # WS_EX_TOPMOST
            assert pet.bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput
            assert pet.bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
            assert work_area(pet.bubble.screen()).contains(pet.bubble.frameGeometry())
            bubble_center = pet.bubble.rect().center()
            # Inspect only the test-owned synthetic bubble, not the desktop.
            bubble_image = pet.bubble.grab().toImage()
            ratio = pet.bubble.devicePixelRatioF()
            assert bubble_image.pixelColor(
                round(bubble_center.x() * ratio), round(bubble_center.y() * ratio),
            ).alpha() > 240, "The click must exercise opaque speech-bubble pixels"
            global_center = pet.bubble.mapToGlobal(bubble_center)
            assert not pet.character_contains(pet.mapFromGlobal(global_center))
            before = state()["clicks"]
            click(native_point(bubble_hwnd, bubble_center, ratio))
            wait_for(lambda: state()["clicks"] == before + 1)
            assert pet.bubble.isVisible()
            assert user32.GetForegroundWindow() == probe["hwnd"]
            results["opaque_speech_bubble_clicks_other_process"] = True
            type_key(0x42)
            wait_for(lambda: state().get("text", "").lower() == "ab")
            results["speech_bubble_retains_other_process_keyboard_focus"] = True
            results["speech_bubble_remains_inside_taskbar_work_area"] = True

            # Only an explicitly linked screen comment accepts native clicks.
            # These fixture UUIDs have no database rows and trigger no API,
            # screen capture or real application-controller navigation.
            conversation_id = "a7c8b1e0-9374-43ca-b259-a9dbb4e41d3e"
            message_id = "77f1ca82-c210-4382-95b5-82fd536b7013"
            linked = QSignalSpy(pet.bubble.conversation_requested)
            pet.bubble.say(
                "这是测试专用的屏幕评论。点击后应发出对应会话与消息标识，"
                "同时保留下面输入框的键盘焦点。",
                3000,
                conversation_link={"conversation_id": conversation_id, "message_id": message_id},
            )
            wait_for(lambda: pet.bubble.isVisible())
            QTest.qWait(100)
            linked_hwnd = int(pet.bubble.winId())
            assert not pet.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert not pet.bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput
            assert pet.bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
            assert user32.GetWindowLongW(linked_hwnd, -20) & 0x00000008  # WS_EX_TOPMOST
            before = state()["clicks"]
            linked_center = pet.bubble._body_rect.center().toPoint()
            click(native_point(linked_hwnd, linked_center, pet.bubble.devicePixelRatioF()))
            wait_for(lambda: linked.count() == 1)
            assert linked.at(0) == [conversation_id, message_id]
            assert state()["clicks"] == before, "A linked comment must consume its own click"
            assert user32.GetForegroundWindow() == probe["hwnd"]
            results["linked_speech_bubble_native_click_emits_exact_conversation_message"] = True
            results["linked_speech_bubble_does_not_click_through"] = True
            type_key(0x43)
            wait_for(lambda: state().get("text", "").lower() == "abc")
            results["linked_speech_bubble_retains_other_process_keyboard_focus"] = True
            # Verify both directions of the native flag transition. Later
            # ordinary feedback must continue to pass clicks through.
            pet.bubble.say("普通反馈恢复鼠标穿透。", 700)
            wait_for(lambda: pet.bubble.isVisible())
            assert pet.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert pet.bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput
            results["ordinary_speech_bubble_restores_native_click_through_after_linked"] = True
            screen = pet.screen()
            # Capture only our own character widget, never the user's desktop.
            assert pet.grab().save(str(artifacts / "desktop_pet_smoke.png"))
            results["screenshot_scope"] = "test-owned pet widget only"

            wait_for(lambda: pet.animation.state == AnimationState.IDLE)
            center = pet.animated_sprite_rect.center().toPoint()
            click(native_point(pet_hwnd, center, pet.devicePixelRatioF()))
            assert pet.animation.state == AnimationState.SURPRISED
            results["second_native_click_triggers_surprised"] = True

            start = native_point(pet_hwnd, pet.animated_sprite_rect.center().toPoint(), pet.devicePixelRatioF())
            old_position = QPoint(pet.pos())
            pointer(*start, 0x0002)
            assert pet.animation.state == AnimationState.DRAGGING
            frozen = pet.animation.pose
            QTest.qWait(120)
            assert pet.animation.pose == frozen
            pointer(start[0] + 70, start[1] + 30)
            assert pet.animation.pose == frozen
            pointer(start[0] + 70, start[1] + 30, 0x0004)
            assert pet.pos() != old_position
            assert pet.animation.state == AnimationState.RETURNING
            wait_for(lambda: pet.animation.state == AnimationState.IDLE)
            wait_for(lambda: not pet.bubble.isVisible())
            QTest.qWait(100)
            assert not pet.bubble.isVisible()
            results["native_drag_while_bubble_visible"] = True
            results["native_drag_freezes_pose_then_returns_to_idle"] = True

            start = native_point(pet_hwnd, pet.animated_sprite_rect.center().toPoint(), pet.devicePixelRatioF())
            pointer(*start, 0x0002)
            # Move to the bottom of the actual monitor, over its taskbar.
            full_area = screen.geometry()
            end = native_point(probe["hwnd"], QPoint(250, full_area.height() - 1), probe["dpr"])
            pointer(*end)
            pointer(*end, 0x0004)
            assert QRectF(work_area(screen)).adjusted(-1e-6, -1e-6, 1e-6, 1e-6).contains(
                pet.character_placement_bounds.translated(pet.pos()))
            assert not pet.bubble.isVisible()
            results["native_drag_clamped_above_taskbar_no_click"] = True
            results["native_visible_edge_gaps"] = {}
            area, desktop = work_area(screen), screen.geometry()
            for edge, destination in (
                ("left", QPoint(desktop.left() + 2, area.center().y())),
                ("right", QPoint(desktop.right() - 2, area.center().y())),
                ("top", QPoint(area.center().x(), desktop.top() + 2)),
                ("bottom", QPoint(area.center().x(), desktop.bottom() - 2)),
            ):
                pet.move((QPointF(area.center()) - pet.character_placement_bounds.center()).toPoint())
                QTest.qWait(80)
                start = native_point(pet_hwnd, pet.animated_sprite_rect.center().toPoint(), pet.devicePixelRatioF())
                end = native_point(pet_hwnd, destination - pet.pos(), pet.devicePixelRatioF())
                pointer(*start, 0x0002)
                pointer(*end)
                pointer(*end, 0x0004)
                painted = pet.character_placement_bounds.translated(pet.pos())
                assert QRectF(area).adjusted(-1e-6, -1e-6, 1e-6, 1e-6).contains(painted)
                gap = {"left": painted.left() - area.x(),
                       "right": area.x() + area.width() - painted.right(),
                       "top": painted.top() - area.y(),
                       "bottom": area.y() + area.height() - painted.bottom()}[edge]
                assert -1e-6 <= gap < 1.000001, (edge, gap)
                assert not pet.bubble.isVisible()
                results["native_visible_edge_gaps"][edge] = round(gap, 6)
            results["native_drag_reaches_all_four_visible_edges"] = True
            results["screen_geometry"] = screen.geometry().getRect()
            results["qt_available_geometry"] = screen.availableGeometry().getRect()
            results["effective_work_area"] = work_area(screen).getRect()
            results["device_pixel_ratio"] = screen.devicePixelRatio()

            if bubble_only:
                results.update({"status": "passed", "stage": "completed",
                                "real_entrypoint_skipped": True})
                return 0

            # Also start the real entry point and prove menu exit ends its process.
            pet.close()
            QTest.qWait(100)
            # The helper used topmost only to obtain initial desktop access.
            # Make it an ordinary foreground app for the separate entry point.
            if not native_call("SetWindowPos", probe["hwnd"], wintypes.HWND(-2), 0, 0, 0, 0, 0x0013):
                raise NativeEnvironmentUnavailable("Could not lower the controlled background probe")
            results["stage"] = "isolated_entrypoint"
            entrypoint_state = Path(temporary) / "entrypoint_pid.json"
            normal_app = subprocess.Popen(
                [sys.executable, str(ROOT / "tests" / "isolated_entrypoint.py"),
                 "--settings-dir", str(Path(temporary) / "entrypoint"), "--state", str(entrypoint_state)], cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            results["entrypoint_configuration_isolated"] = True
            found = []
            owned_pids = set()
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL

            @callback_type
            def visit(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                # Never select the user's running pet even if titles match.
                if owner.value not in owned_pids:
                    return True
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, len(title))
                if title.value == "AURA · 鲸鱼娘桌宠" and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            def find_pet():
                owned_pids.clear()
                owned_pids.update(controlled_process_ids(normal_app.pid))
                found.clear()
                user32.EnumWindows(visit, 0)
                return bool(found)

            wait_for(lambda: entrypoint_state.exists())
            published = json.loads(entrypoint_state.read_text(encoding="utf-8"))
            assert published["configuration_isolated"] and published["credential_backend"] == "in_memory"
            assert published["pid"] in controlled_process_ids(normal_app.pid)
            wait_for(find_pet)
            app_hwnd = found[0]
            app_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(app_hwnd, ctypes.byref(app_pid))
            allowed_input_pids.add(app_pid.value)
            if not native_call("SetWindowPos", app_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053):
                raise NativeEnvironmentUnavailable("Could not raise the isolated entrypoint window")

            def choose_menu_action(label, *, accessible=False):
                windows = []

                @callback_type
                def inspect_menu(hwnd, _):
                    owner = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                    if owner.value == app_pid.value and user32.IsWindowVisible(hwnd):
                        windows.append(hwnd)
                    return True

                user32.EnumWindows(inspect_menu, 0)
                matches = []
                diagnostics = []
                for hwnd in windows:
                    if hwnd == app_hwnd:
                        continue
                    elements = read_dialog_elements(hwnd)
                    diagnostics.append((hwnd, [element["name"] for element in elements]))
                    matches.extend((hwnd, element) for element in elements
                                   if element["name"] == label
                                   and element["width"] > 0 and element["height"] > 0)
                if not matches:
                    print("Native menu accessibility:", windows, diagnostics)
                assert matches, f"The native menu did not expose action: {label}"
                menu_hwnd, item = matches[0]
                if accessible:
                    return invoke_accessible_menu_action(menu_hwnd, label)
                if not native_call("SetWindowPos", menu_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053):
                    raise NativeEnvironmentUnavailable("Could not raise the controlled application menu")
                click((round(item["left"] + item["width"] / 2),
                       round(item["top"] + item["height"] / 2)))

            def panel_window(prefix):
                matches = []

                @callback_type
                def inspect(hwnd, _):
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value != app_pid.value:
                        return True
                    title = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if title.value.startswith(prefix) and user32.IsWindowVisible(hwnd):
                        matches.append(hwnd)
                    return True

                user32.EnumWindows(inspect, 0)
                return matches[0] if matches else None

            # This entrypoint uses only the isolated fixture's default scale.
            dpr = QGuiApplication.primaryScreen().devicePixelRatio()
            app_client = wintypes.RECT()
            user32.GetClientRect(app_hwnd, ctypes.byref(app_client))
            torso = QPoint(round(app_client.right / dpr / 2),
                           round(app_client.bottom / dpr / 2))
            # A layered HWND may become visible before its first opaque paint.
            # Wait for native hit testing, not merely IsWindowVisible readiness.
            torso_point = native_point(app_hwnd, torso, dpr)
            def pet_ready_for_input():
                hit = user32.WindowFromPoint(wintypes.POINT(*torso_point))
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hit, ctypes.byref(owner))
                return owner.value == app_pid.value
            wait_for(pet_ready_for_input)
            for attempt in range(2):
                click(native_point(app_hwnd, torso, dpr), right=True)
                choose_menu_action("打开调试面板")
                try:
                    wait_for(lambda: panel_window("鲸鱼娘 · 调试面板") is not None)
                except AssertionError:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(app_hwnd, ctypes.byref(rect))
                    print("Panel menu diagnostic:", "attempt", attempt, "pet native rectangle",
                          (rect.left, rect.top, rect.right, rect.bottom), "pid", app_pid.value,
                          "foreground", user32.GetForegroundWindow())
                    raise
                debug_hwnd = panel_window("鲸鱼娘 · 调试面板")
                if attempt == 0:
                    first_debug_hwnd = debug_hwnd
                else:
                    assert debug_hwnd == first_debug_hwnd
                user32.PostMessageW(debug_hwnd, 0x0010, 0, 0)  # WM_CLOSE hides this panel
                wait_for(lambda: not user32.IsWindowVisible(debug_hwnd))
                assert user32.IsWindowVisible(app_hwnd) and normal_app.poll() is None
            results["native_menu_debug_panel_close_reopen_same_window"] = True
            click(native_point(app_hwnd, torso, dpr), right=True)
            choose_menu_action("打开聊天窗口")
            wait_for(lambda: panel_window("和鲸鱼娘聊天") is not None)
            chat_hwnd = panel_window("和鲸鱼娘聊天")
            user32.PostMessageW(chat_hwnd, 0x0010, 0, 0)
            wait_for(lambda: not user32.IsWindowVisible(chat_hwnd))
            assert user32.IsWindowVisible(app_hwnd)
            results["native_menu_independent_chat_closes_without_pet_exit"] = True
            click(native_point(app_hwnd, torso, dpr), right=True)
            QTest.qWait(250)
            exit_action = choose_menu_action("退出桌宠", accessible=True)
            wait_for(lambda: normal_app.poll() is not None)
            assert normal_app.returncode == 0
            results["real_entrypoint_right_menu_exits_process"] = True
            results["menu_exit_method"] = exit_action["method"]
            results["menu_exit_action"] = exit_action["name"]
            results["menu_exit_physical_click_verified"] = False
        results.update({"status": "passed", "stage": "completed"})
        return 0
    except NativeEnvironmentUnavailable as error:
        results.update({"status": "incomplete", "reason": str(error)})
        return 2
    except Exception as error:
        results.update({"status": "failed", "error": str(error)})
        return 1
    finally:
        for button in held_mouse_buttons:
            release = Input(type=0, mi=MouseInput(dwFlags={0x0002: 0x0004, 0x0008: 0x0010}[button]))
            native_call("SendInput", 1, ctypes.byref(release), ctypes.sizeof(Input))
        if pet is not None:
            pet.close()
        for process in (normal_app, probe_process):
            if process is not None and process.poll() is None:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                process.wait(timeout=5)
        if cursor_available:
            results["cursor_restored"] = bool(native_call("SetCursorPos", original_cursor.x, original_cursor.y))
        (artifacts / "windows_smoke.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        raise SystemExit(run_probe(Path(sys.argv[2])))
    if len(sys.argv) > 1 and sys.argv[1:] != ["--bubble-only"]:
        raise SystemExit("Usage: windows_smoke.py [--bubble-only]")
    raise SystemExit(run_validation(bubble_only="--bubble-only" in sys.argv))
