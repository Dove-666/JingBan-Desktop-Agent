"""Dump-before-action UI helper restricted to the owned emulator-5560 release."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

ADB = r"E:\Android\Sdk\platform-tools\adb.exe"
SERIAL = "emulator-5560"
OUTPUT = Path("artifacts/qa-phone-release-stream")


def adb(*args):
    return subprocess.check_output([ADB, "-s", SERIAL, *args], timeout=25, stderr=subprocess.STDOUT)


def snapshot(label):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = str(time.time_ns()) + "-" + re.sub(r"[^a-zA-Z0-9_-]", "_", label)
    dumped = adb("shell", "uiautomator", "dump", "/sdcard/aura-qa-release-current.xml")
    if b"dumped to:" not in dumped:
        # Never mistake the previous successful dump for a fresh observation
        # while Android is between windows or reports a null accessibility root.
        raise ValueError("QA_UI_DUMP_UNAVAILABLE")
    raw = adb("shell", "cat", "/sdcard/aura-qa-release-current.xml")
    tree = ET.fromstring(raw)
    nodes = list(tree.iter("node"))
    for node in nodes:
        if node.get("password") == "true":
            node.set("text", "[qa-password-hidden]")
    (OUTPUT / (stamp + ".xml")).write_bytes(ET.tostring(tree, encoding="utf-8"))
    (OUTPUT / (stamp + ".png")).write_bytes(adb("exec-out", "screencap", "-p"))
    interesting = [{key: node.get(key, "") for key in ("text", "content-desc", "class", "bounds", "focused", "enabled", "clickable")}
                   for node in nodes if node.get("text") or node.get("content-desc") or "EditText" in node.get("class", "")]
    return nodes, {"snapshot": stamp, "utc": datetime.now(timezone.utc).isoformat(), "nodes": interesting}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "tap", "type", "back"))
    parser.add_argument("--label", default="ui")
    parser.add_argument("--text")
    parser.add_argument("--description")
    parser.add_argument("--class-name")
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    if adb("get-state").strip() != b"device" or b"aura_api35" not in adb("emu", "avd", "name"):
        raise SystemExit("QA_AVD_IDENTITY_REQUIRED")
    nodes, record = snapshot(args.label)
    record["action"] = args.action
    if args.action == "tap":
        matches = [node for node in nodes if (args.text is None or node.get("text") == args.text)
                   and (args.description is None or node.get("content-desc") == args.description)
                   and (args.class_name is None or node.get("class") == args.class_name)]
        if not (args.text is not None or args.description is not None or args.class_name is not None) or not 0 <= args.index < len(matches):
            raise SystemExit("QA_TARGET_NOT_FOUND")
        node = matches[args.index]
        values = list(map(int, re.findall(r"\d+", node.get("bounds", ""))))
        if len(values) != 4 or node.get("enabled") != "true":
            raise SystemExit("QA_TARGET_UNAVAILABLE")
        x1, y1, x2, y2 = values
        adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
        record["target_bounds"] = node.get("bounds")
    elif args.action == "type":
        if not any(node.get("focused") == "true" and "EditText" in node.get("class", "") for node in nodes):
            raise SystemExit("QA_FOCUSED_EDITOR_REQUIRED")
        if not args.text or not re.fullmatch(r"[A-Za-z0-9_:/.-]+", args.text):
            raise SystemExit("QA_SYNTHETIC_ASCII_ONLY")
        adb("shell", "input", "keycombination", "113", "29")
        adb("shell", "input", "text", args.text)
        record["typed_characters"] = len(args.text)
    elif args.action == "back":
        adb("shell", "input", "keyevent", "4")
    with (OUTPUT / "ui_steps.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=True))
