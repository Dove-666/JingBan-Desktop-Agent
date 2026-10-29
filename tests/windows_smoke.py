"""Opt-in real Windows input test. Temporarily moves the mouse; no extra packages.

Run: .venv\Scripts\python.exe tests\windows_smoke.py
Uses a separate background process to test actual transparent click-through.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget

from desktop_pet.config import PetConfig
from desktop_pet.animation_controller import AnimationState
from desktop_pet.pet_window import PetWindow
from desktop_pet.windows_work_area import work_area
from tests.missing_image_smoke import read_dialog_elements


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
                     "hwnd": int(self.winId()), "dpr": self.devicePixelRatioF()}
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


def run_validation() -> int:
    if sys.platform != "win32":
        raise SystemExit("This test requires an interactive Windows desktop.")
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    original_cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(original_cursor))
    results = {}
    pet = None
    normal_app = None
    probe_process = None
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)

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
        assert user32.SetCursorPos(x, y)
        if flag is not None:
            user32.mouse_event(flag, 0, 0, 0, 0)
        QTest.qWait(70)

    def click(point, right=False):
        pointer(*point, 0x0008 if right else 0x0002)
        pointer(*point, 0x0010 if right else 0x0004)

    try:
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
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetWindowPos(probe["hwnd"], wintypes.HWND(-1), 0, 0, 0, 0, 0x0053)
            user32.SetForegroundWindow(probe["hwnd"])
            click(native_point(probe["hwnd"], QPoint(70, 86), probe["dpr"]))
            try:
                wait_for(lambda: user32.GetForegroundWindow() == probe["hwnd"])
            except AssertionError:
                foreground = user32.GetForegroundWindow()
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(foreground, title, len(title))
                print("Initial focus diagnostic:", probe, "foreground", foreground, title.value)
                raise

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
            user32.keybd_event(0x41, 0, 0, 0)
            user32.keybd_event(0x41, 0, 0x0002, 0)
            wait_for(lambda: state().get("text", "").lower() == "a")
            results["click_bubble_and_background_keyboard_focus"] = True
            screen = pet.screen()
            assert screen.grabWindow(0).save(str(artifacts / "desktop_pet_smoke.png"))

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
            assert work_area(screen).contains(pet.frameGeometry())
            assert not pet.bubble.isVisible()
            results["native_drag_clamped_above_taskbar_no_click"] = True
            results["screen_geometry"] = screen.geometry().getRect()
            results["qt_available_geometry"] = screen.availableGeometry().getRect()
            results["effective_work_area"] = work_area(screen).getRect()
            results["device_pixel_ratio"] = screen.devicePixelRatio()

            # Also start the real entry point and prove menu exit ends its process.
            pet.close()
            QTest.qWait(100)
            # The helper used topmost only to obtain initial desktop access.
            # Make it an ordinary foreground app for the separate entry point.
            user32.SetWindowPos(probe["hwnd"], wintypes.HWND(-2), 0, 0, 0, 0, 0x0013)
            normal_app = subprocess.Popen(
                [sys.executable, str(ROOT / "main.py")], cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            found = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @callback_type
            def visit(hwnd, _):
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, len(title))
                # Windows venv python.exe may launch an interpreter child process.
                if title.value == "AURA · 鲸鱼娘桌宠" and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            def find_pet():
                found.clear()
                user32.EnumWindows(visit, 0)
                return bool(found)

            wait_for(find_pet)
            app_hwnd = found[0]
            app_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(app_hwnd, ctypes.byref(app_pid))
            user32.SetWindowPos(app_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053)

            def choose_menu_action(label):
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
                    QGuiApplication.primaryScreen().grabWindow(0).save(str(artifacts / "native_menu_diagnostic.png"))
                assert matches, f"The native menu did not expose action: {label}"
                menu_hwnd, item = matches[0]
                user32.SetWindowPos(menu_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053)
                click((round(item["left"] + item["width"] / 2),
                       round(item["top"] + item["height"] / 2)))

            def panel_window(prefix):
                matches = []

                @callback_type
                def inspect(hwnd, _):
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    title = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if pid.value == app_pid.value and title.value.startswith(prefix) and user32.IsWindowVisible(hwnd):
                        matches.append(hwnd)
                    return True

                user32.EnumWindows(inspect, 0)
                return matches[0] if matches else None

            # Default center is opaque; Qt uses the same primary display scale.
            dpr = QGuiApplication.primaryScreen().devicePixelRatio()
            # A layered HWND may become visible before its first opaque paint.
            # Wait for native hit testing, not merely IsWindowVisible readiness.
            torso_point = native_point(app_hwnd, QPoint(180, 230), dpr)
            def pet_ready_for_input():
                hit = user32.WindowFromPoint(wintypes.POINT(*torso_point))
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hit, ctypes.byref(owner))
                return owner.value == app_pid.value
            wait_for(pet_ready_for_input)
            for attempt in range(2):
                click(native_point(app_hwnd, QPoint(180, 230), dpr), right=True)
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
            click(native_point(app_hwnd, QPoint(180, 230), dpr), right=True)
            choose_menu_action("打开聊天窗口")
            wait_for(lambda: panel_window("和鲸鱼娘聊天") is not None)
            chat_hwnd = panel_window("和鲸鱼娘聊天")
            user32.PostMessageW(chat_hwnd, 0x0010, 0, 0)
            wait_for(lambda: not user32.IsWindowVisible(chat_hwnd))
            assert user32.IsWindowVisible(app_hwnd)
            results["native_menu_independent_chat_closes_without_pet_exit"] = True
            click(native_point(app_hwnd, QPoint(180, 230), dpr), right=True)
            QTest.qWait(250)
            choose_menu_action("退出桌宠")
            wait_for(lambda: normal_app.poll() is not None)
            assert normal_app.returncode == 0
            results["real_entrypoint_right_menu_exits_process"] = True
        (artifacts / "windows_smoke.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    finally:
        if pet is not None:
            pet.close()
        for process in (normal_app, probe_process):
            if process is not None and process.poll() is None:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                process.wait(timeout=5)
        user32.SetCursorPos(original_cursor.x, original_cursor.y)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        raise SystemExit(run_probe(Path(sys.argv[2])))
    raise SystemExit(run_validation())
