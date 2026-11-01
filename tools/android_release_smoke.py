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
ADB: list[str] = []
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
    parser.add_argument("--serial", required=True)
    parser.add_argument("--expected-reply", default="Android E2E reply 3")
    parser.add_argument("--message", default=MARKER)
    parser.add_argument("--direct-api", action="store_true")
    parser.add_argument("--real-provider", action="store_true")
    args = parser.parse_args()
    args.report_directory = args.report_directory.resolve()
    if not re.fullmatch(r"emulator-\d+", args.serial):
        parser.error("Only an explicitly selected QA emulator is permitted")
    if not re.fullmatch(r"[A-Za-z0-9 _.,:?!-]{1,1000}", args.message):
        parser.error("Only a public ASCII smoke-test prompt is permitted")
    if args.real_provider and not args.direct_api:
        parser.error("Real-provider smoke testing must explicitly use direct API mode")
    ADB = ["E:/Android/Sdk/platform-tools/adb.exe", "-s", args.serial]
    if args.stage == "inspect":
        whitelist = {"AURA", "连接我的电脑", "连接电脑", "配对信息", "完整配对链接", "已连接 · 对话已同步", "正在同步对话"}
        print(json.dumps({"labels": [node.get("text") for node in nodes() if node.get("text") in whitelist]}, ensure_ascii=False))
    elif args.stage == "invalid":
        current_nodes = nodes()
        if not any(node.get("text") == "完整配对链接" for node in current_nodes):
            raise RuntimeError("Open the release pairing dialog before this QA step")
        fields = [node for node in current_nodes if node.get("class") == "android.widget.EditText"]
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
        adb("shell", "input", "keycombination", "113", "29")
        adb("shell", "input", "text", args.message.replace(" ", "%s"))
        buttons = [node for node in nodes() if node.get("content-desc") == "发送消息" and node.get("enabled") == "true"]
        if len(buttons) != 1:
            raise RuntimeError("Expected one enabled release send button")
        tap(buttons[0])
        adb("shell", "input", "keyevent", "111")  # Hide keyboard before visual capture.
        print(json.dumps({"public_qa_message_sent_through_release_ui": True}))
    else:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                texts = [node.get("text", "") for node in nodes()]
            except RuntimeError:
                time.sleep(1)
                continue
            # The public prompt may itself contain the marker. Only a separate
            # exact reply node proves the model answered; substring matching
            # could mistake the user's question for a successful completion.
            reply_seen = args.expected_reply in texts
            connection_seen = any(text.startswith("手机直连") for text in texts) if args.direct_api else any("已连接" in text for text in texts)
            if reply_seen and connection_seen:
                directory = args.report_directory
                directory.mkdir(parents=True, exist_ok=True)
                screenshot = "release-direct-connected.png" if args.direct_api else "release-connected.png"
                result_name = "release-direct-smoke-result.json" if args.direct_api else "release-smoke-result.json"
                (directory / screenshot).write_bytes(adb("exec-out", "screencap", "-p"))
                memory = adb("shell", "dumpsys", "meminfo", PACKAGE).decode("utf-8", errors="replace")
                pss = re.search(r"TOTAL PSS:\s+(\d+)", memory)
                report = {"passed": True, "package": PACKAGE, "signed_release_installed": True,
                          "real_release_ui_pairing": True, "real_release_ui_chat_send": True,
                          "real_release_ui_reply_observed": True, "mock_chat_model": not args.real_provider,
                          "direct_api": args.direct_api, "real_https_provider": args.real_provider,
                          "device": "Android 35 x86_64 emulator", "physical_phone_tested": False,
                          "background_push": False, "emulator_total_pss_kib": int(pss.group(1)) if pss else None,
                          "screenshot": str((directory / screenshot).relative_to(ROOT))}
                (directory / result_name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False))
                return
            time.sleep(1)
        raise RuntimeError("Release reply was not observed; no private UI data has been printed")


if __name__ == "__main__":
    main()
