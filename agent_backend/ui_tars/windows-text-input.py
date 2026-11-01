"""One fixed Unicode SendInput operation over private stdin; no shell/clipboard."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import sys


def main():
    if sys.platform != "win32":
        raise ValueError()
    request = json.loads(sys.stdin.buffer.read(262_145).decode("utf-8"))
    if not isinstance(request, dict) or set(request) != {"text"}:
        raise ValueError()
    text = request["text"]
    if not isinstance(text, str) or len(text) > 65_536 or "\0" in text:
        raise ValueError()
    # Validate the entire operation before dispatching its first character.
    text.encode("utf-16-le", errors="strict")
    pointer = wintypes.WPARAM
    class Keyboard(ctypes.Structure):
        _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", pointer)]
    class Mouse(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG), ("data", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", pointer)]
    class Hardware(ctypes.Structure):
        _fields_ = [("message", wintypes.DWORD), ("low", wintypes.WORD), ("high", wintypes.WORD)]
    class Payload(ctypes.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse), ("hardware", Hardware)]
    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("payload", Payload)]
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    foreground = user.GetForegroundWindow()
    if not foreground:
        raise RuntimeError()
    send = user.SendInput
    send.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    send.restype = wintypes.UINT
    def event(vk=0, scan=0, flags=0):
        return Input(1, Payload(keyboard=Keyboard(vk, scan, flags, 0, 0)))
    for character in text.replace("\r\n", "\n").replace("\r", "\n"):
        if user.GetForegroundWindow() != foreground:
            raise RuntimeError()
        if character == "\n":
            events = [event(vk=0x0D), event(vk=0x0D, flags=0x0002)]
        else:
            encoded = character.encode("utf-16-le")
            units = [int.from_bytes(encoded[index:index+2], "little") for index in range(0, len(encoded), 2)]
            events = [item for unit in units for item in (event(scan=unit, flags=0x0004), event(scan=unit, flags=0x0006))]
        sequence = (Input * len(events))(*events)
        # A partial SendInput is an uncertain outcome, never retried here.
        if send(len(events), sequence, ctypes.sizeof(Input)) != len(events):
            raise RuntimeError()
    print('{"ok":true}', flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"ok":false,"code":"INPUT_FAILED"}', flush=True)
        raise SystemExit(1)
