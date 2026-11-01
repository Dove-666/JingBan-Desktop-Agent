"""Native Messaging host: a browser selection opens a local question composer.

Only framed JSON is written to stdout. No API credential, page fetch, clipboard
access or model request belongs in this short-lived process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXTENSION_ID = "gbeikpndpckhjeakbckijelidpdpinei"
ALLOWED_ORIGIN = "chrome-extension://" + EXTENSION_ID + "/"
MAX_BYTES = 96 * 1024


class NativeMessageError(ValueError):
    pass


def _read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        part = stream.read(count - len(chunks))
        if not part:
            raise NativeMessageError("浏览器发送的选区请求不完整。")
        chunks.extend(part)
    return bytes(chunks)


def read_message(stream):
    length = struct.unpack("<I", _read_exact(stream, 4))[0]
    if not 0 < length <= MAX_BYTES:
        raise NativeMessageError("选区内容太长，请缩小选择范围后重试。")
    try:
        message = json.loads(_read_exact(stream, length).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise NativeMessageError("无法读取浏览器选区，请重新选择文字后再试。") from None
    if (not isinstance(message, dict) or set(message) != {"version", "action", "context"}
            or type(message.get("version")) is not int or message["version"] != 1
            or message.get("action") != "ask_selection" or not isinstance(message.get("context"), dict)):
        raise NativeMessageError("浏览器选区请求格式无效。")
    return message


def write_message(stream, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > 4096:
        payload = b'{"ok":false,"error":"Unable to open the question window."}'
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def default_config_path():
    from PySide6.QtCore import QCoreApplication, QStandardPaths
    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / "settings.ini"


def start_pet(config_path):
    # The existing app CLI uses its ordinary per-user settings. Do not launch
    # another configuration accidentally when a developer supplied a custom one.
    if Path(config_path).resolve() != default_config_path().resolve():
        return False
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").is_file():
        executable = executable.with_name("pythonw.exe")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen([str(executable), str(ROOT / "main.py")], cwd=str(ROOT),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        return False
    return True


def handle_message(message, config_path, *, deliver=None, launch=None, timeout_seconds=9):
    from PySide6.QtCore import QCoreApplication
    from desktop_pet.selection.context import SelectionContext
    from desktop_pet.selection.ipc import deliver_selection
    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    context = SelectionContext.from_dict(message["context"])
    request = {"version": 1, "action": "ask_selection", "request_id": str(uuid4()), "context": context.to_dict()}
    deliver = deliver or deliver_selection
    launch = launch or start_pet
    # First-show font/widget initialization can take longer than a short IPC
    # probe. Wait for the actual composer acknowledgement, even on a busy PC.
    result = deliver(config_path, request, timeout_ms=2500)
    if result.get("ok"):
        return {"ok": True}
    # A connected inbox can also decline because its composer limit is reached.
    # Launch only when there is no listener, never restart an already running pet.
    if result.get("error") != "桌宠尚未就绪，请启动 AURA 后再试。":
        return {"ok": False, "error": result.get("error") or "提问框暂时无法打开，请稍后再试。"}
    if not launch(config_path):
        return {"ok": False, "error": "无法自动启动桌宠，请先启动 AURA 再使用右键提问。"}
    deadline = time.monotonic() + max(0.1, min(timeout_seconds, 12))
    while time.monotonic() < deadline:
        result = deliver(config_path, request, timeout_ms=2500)
        if result.get("ok"):
            return {"ok": True}
        if result.get("error") != "桌宠尚未就绪，请启动 AURA 后再试。":
            return {"ok": False, "error": result.get("error") or "提问框暂时无法打开，请稍后再试。"}
        time.sleep(0.05)
    return {"ok": False, "error": "桌宠尚未接通右键提问。如果正在运行旧版本，请右键退出并重新启动 AURA，再选中文字提问。"}


def main(argv=None, *, input_stream=None, output_stream=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config-path", type=Path)
    options, browser_args = parser.parse_known_args(argv)
    source = input_stream if input_stream is not None else sys.stdin.buffer
    destination = output_stream if output_stream is not None else sys.stdout.buffer
    if os.name == "nt" and input_stream is None and output_stream is None:
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    if (not browser_args or browser_args[0] != ALLOWED_ORIGIN
            or any(not argument.startswith("--parent-window=") for argument in browser_args[1:])):
        write_message(destination, {"ok": False, "error": "浏览器扩展身份未通过验证，请重新安装问问鲸鱼娘扩展。"})
        return 1
    try:
        message = read_message(source)
        config_path = options.config_path if options.config_path is not None else default_config_path()
        result = handle_message(message, config_path)
    except NativeMessageError as error:
        result = {"ok": False, "error": str(error)}
    except Exception:
        result = {"ok": False, "error": "本地提问桥暂时不可用，请检查项目依赖并重新安装浏览器连接。"}
    write_message(destination, result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
