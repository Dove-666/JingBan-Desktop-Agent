"""Opt-in native Windows validation of the real missing-image error dialog.

Run: .venv\Scripts\python.exe tests\missing_image_smoke.py
Temporarily moves the mouse. Uses Windows UI Automation through built-in
PowerShell to read Qt dialog descendants; no additional packages are needed.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def read_dialog_elements(hwnd: int) -> list[dict]:
    # Qt labels may be alien widgets, with no individual Win32 child HWND.
    # UI Automation exposes their actual text across the process boundary.
    script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]HWND_VALUE)
$elements = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                         [System.Windows.Automation.Condition]::TrueCondition)
$result = @()
foreach ($element in $elements) {
    $current = $element.Current
    $rect = $current.BoundingRectangle
    $result += @{
        name = $current.Name
        control_type = $current.ControlType.ProgrammaticName
        native_handle = $current.NativeWindowHandle
        left = $rect.Left
        top = $rect.Top
        width = $rect.Width
        height = $rect.Height
    }
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $result -Depth 4 -Compress))
""".replace("HWND_VALUE", str(hwnd))
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=12, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode:
        raise AssertionError(f"Windows UI Automation failed: {completed.stderr}")
    try:
        return json.loads(completed.stdout.strip().lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Invalid UI Automation output: {completed.stdout!r}") from exc


def run_validation() -> int:
    if sys.platform != "win32":
        raise SystemExit("This test requires an interactive Windows desktop.")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    original_cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(original_cursor))

    def find_error_dialogs():
        handles = []

        @callback_type
        def callback(hwnd, _):
            text = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, text, len(text))
            # Qt appends the application display name to native dialog titles.
            if user32.IsWindowVisible(hwnd) and text.value.startswith("桌宠启动失败"):
                handles.append((int(hwnd), text.value))
            return True

        user32.EnumWindows(callback, 0)
        return handles

    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="aura-missing-image-") as directory:
            missing = Path(directory).resolve() / "missing-character.png"
            environment = dict(os.environ, PYTHONIOENCODING="utf-8")
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "main.py"), "--image", str(missing)],
                cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            deadline = time.monotonic() + 12
            dialog = None
            dialog_title = ""
            elements = []
            while time.monotonic() < deadline and process.poll() is None:
                for hwnd, title in find_error_dialogs():
                    candidate = read_dialog_elements(hwnd)
                    text = "\n".join(element["name"] for element in candidate)
                    # A unique temporary path ensures this is our own dialog,
                    # including if .venv Python spawned the visible child process.
                    if str(missing) in text and "找不到桌宠图片" in text:
                        dialog, dialog_title, elements = hwnd, title, candidate
                        break
                if dialog is not None:
                    break
                time.sleep(0.05)
            if dialog is None:
                raise AssertionError("The actual missing-image error dialog was not found with expected text.")

            buttons = [
                element for element in elements
                if element["control_type"] == "ControlType.Button"
                and element["width"] > 0 and element["height"] > 0
            ]
            if len(buttons) != 1:
                raise AssertionError(f"Expected one confirmation button, found: {buttons!r}")
            button = buttons[0]
            # Make only our uniquely identified test dialog reachable over any
            # third-party topmost desktop window before sending a native click.
            user32.SetWindowPos(dialog, wintypes.HWND(-1), 0, 0, 0, 0, 0x0053)
            user32.SetForegroundWindow(dialog)
            user32.SetCursorPos(
                round(button["left"] + button["width"] / 2),
                round(button["top"] + button["height"] / 2),
            )
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            time.sleep(0.08)
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            stdout, stderr = process.communicate(timeout=6)
            if process.returncode != 2:
                raise AssertionError(f"Expected exit code 2, got {process.returncode}: {stderr}")
            if "桌宠启动失败" not in stderr or "找不到桌宠图片" not in stderr or str(missing) not in stderr:
                raise AssertionError(f"stderr did not contain the actionable missing-image error: {stderr}")

            result = {
                "passed": True,
                "dialog_title": dialog_title,
                "dialog_text": [element["name"] for element in elements if element["name"]],
                "confirmation_button": button["name"],
                "confirmation_method": "native mouse click",
                "exit_code": process.returncode,
                "stderr": stderr,
                "stdout": stdout,
            }
            artifacts = ROOT / "artifacts"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "missing_image_smoke.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    finally:
        user32.SetCursorPos(original_cursor.x, original_cursor.y)
        if process is not None and process.poll() is None:
            # taskkill /T also handles a .venv launcher and its Python child.
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            process.communicate(timeout=5)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run_validation())
