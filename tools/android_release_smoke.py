"""Operate only the isolated emulator release smoke test; never print pairing data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
ADB = ["E:/Android/Sdk/platform-tools/adb.exe", "-s", "emulator-5554"]
PACKAGE = "com.aura.companion"
MARKER = "ANDROID_RELEASE_QA"


def adb(*args: str) -> bytes:
    result = subprocess.run(ADB + list(args), capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("ADB operation failed; raw output withheld to protect private pairing data")
    return result.stdout


def hierarchy() -> ET.Element:
    # Send XML directly to the host process's memory, never shared device files.
    source = adb("exec-out", "uiautomator", "dump", "/dev/tty")
    start = source.find(b"<?xml")
    end = source.find(b"</hierarchy>", start)
    if start < 0 or end < 0:
        raise RuntimeError("Release app hierarchy is unavailable")
    return ET.fromstring(source[start:end + len(b"</hierarchy>")])


def nodes():
    return [node for node in hierarchy().iter("node") if node.get("package") == PACKAGE]


def tap(node: ET.Element) -> None:
    values = list(map(int, re.findall(r"\d+", node.get("bounds", ""))))
    if len(values) != 4:
        raise RuntimeError("Cannot locate the requested public UI control")
    adb("shell", "input", "tap", str((values[0] + values[2]) // 2), str((values[1] + values[3]) // 2))


def main() -> None:
    global ADB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["inspect", "invalid", "confirm", "send", "verify"])
    parser.add_argument("--report-directory", type=Path, default=ROOT / "artifacts/android/qa")
    parser.add_argument("--serial", default="emulator-5554")
    args = parser.parse_args()
    if not re.fullmatch(r"emulator-\d+", args.serial):
        parser.error("Only an explicitly selected QA emulator is permitted")
    ADB = ["E:/Android/Sdk/platform-tools/adb.exe", "-s", args.serial]
    if args.stage == "inspect":
        whitelist = {"AURA", "连接我的电脑", "连接电脑", "配对信息", "完整配对链接", "已连接 · 对话已同步", "正在同步对话"}
        print(json.dumps({"labels": [node.get("text") for node in nodes() if node.get("text") in whitelist]}, ensure_ascii=False))
    elif args.stage == "invalid":
        fields = [node for node in nodes() if node.get("class") == "android.widget.EditText"]
        if len(fields) != 1:
            raise RuntimeError("Expected a single release pairing input")
        tap(fields[0])
        adb("shell", "input", "keycombination", "113", "29")  # Ctrl+A selects the temporary prefill.
        adb("shell", "input", "text", "SHORT_CODE_ONLY_QA_123456")
        adb("shell", "input", "keyevent", "111")
        buttons = [node for node in nodes() if node.get("text") == "连接电脑" and node.get("enabled") == "true"]
        if len(buttons) != 1:
            raise RuntimeError("Expected release pairing confirmation")
        tap(buttons[0])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            texts = [node.get("text", "") for node in nodes()]
            if any("这只是短配对码" in text for text in texts):
                directory = args.report_directory
                directory.mkdir(parents=True, exist_ok=True)
                # Only the public dummy short code is visible at this point.
                (directory / "release-invalid-input.png").write_bytes(adb("exec-out", "screencap", "-p"))
                report = {"passed": True, "real_release_short_code_error": True,
                          "generic_connection_error_replaced": True, "input_contains_only_public_dummy": True}
                (directory / "release-invalid-input-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps(report))
                return
        raise RuntimeError("Expected actionable short-code validation was not observed")
    elif args.stage == "confirm":
        buttons = [node for node in nodes() if node.get("text") == "连接电脑" and node.get("enabled") == "true"]
        if len(buttons) != 1:
            raise RuntimeError("Expected one enabled release pairing confirmation")
        tap(buttons[0])
        print(json.dumps({"explicit_pairing_confirmation_clicked": True}))
    elif args.stage == "send":
        inputs = [node for node in nodes() if node.get("content-desc") == "消息内容"]
        if len(inputs) != 1:
            raise RuntimeError("Expected the connected release composer")
        tap(inputs[0])
        adb("shell", "input", "text", MARKER)
        buttons = [node for node in nodes() if node.get("content-desc") == "发送消息" and node.get("enabled") == "true"]
        if len(buttons) != 1:
            raise RuntimeError("Expected one enabled release send button")
        tap(buttons[0])
        adb("shell", "input", "keyevent", "111")  # Hide keyboard before visual capture.
        print(json.dumps({"public_qa_message_sent_through_release_ui": True}))
    else:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            texts = [node.get("text", "") for node in nodes()]
            if "Android E2E reply 3" in texts:
                directory = args.report_directory
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "release-connected.png").write_bytes(adb("exec-out", "screencap", "-p"))
                memory = adb("shell", "dumpsys", "meminfo", PACKAGE).decode("utf-8", errors="replace")
                pss = re.search(r"TOTAL PSS:\s+(\d+)", memory)
                report = {"passed": True, "package": PACKAGE, "signed_release_installed": True,
                          "real_release_ui_pairing": True, "real_release_ui_chat_send": True,
                          "real_release_ui_synced_reply": True, "mock_chat_model": True,
                          "device": "Android 35 x86_64 emulator", "physical_phone_tested": False,
                          "background_push": False, "emulator_total_pss_kib": int(pss.group(1)) if pss else None,
                          "screenshot": str((directory / "release-connected.png").relative_to(ROOT))}
                (directory / "release-smoke-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False))
                return
            time.sleep(1)
        raise RuntimeError("Release reply was not observed; no private UI data has been printed")


if __name__ == "__main__":
    main()
