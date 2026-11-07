"""Real release UI + local HTTPS/SSE, restricted to the owned QA emulator.

Temporarily overlays the QA AVD's CA store, preserving every original root.
Always attempts to restore that exact overlay and stop the local fixture. No
physical phone, Windows trust, real provider, real keys, or app data reset.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.request

import qa_phone_release_ui as ui
import qa_phone_stream_server as fixture


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "com.aura.companion"
STORE = "/apex/com.android.conscrypt/cacerts"
PORT = 18763


def wait_for(check, code, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError(code)


def tap(label, *, text=None, description=None, class_name=None):
    nodes, _ = ui.snapshot(label)
    selected = [node for node in nodes if (text is None or node.get("text") == text)
                and (description is None or node.get("content-desc") == description)
                and (class_name is None or node.get("class") == class_name)
                and node.get("enabled") == "true"]
    if len(selected) != 1:
        raise RuntimeError("QA_UI_TARGET_NOT_UNIQUE")
    coordinates = list(map(int, re.findall(r"\d+", selected[0].get("bounds", ""))))
    if len(coordinates) != 4:
        raise RuntimeError("QA_UI_BOUNDS_INVALID")
    x1, y1, x2, y2 = coordinates
    ui.adb("shell", "input", "tap", str((x1+x2)//2), str((y1+y2)//2))


def send(prompt):
    if prompt not in {"QA_FIRST", "QA_CANCEL", "QA_AFTER"}:
        raise RuntimeError("QA_PROMPT_REQUIRED")
    tap("focus-"+prompt, class_name="android.widget.EditText")
    ui.adb("shell", "input", "keycombination", "113", "29")
    ui.adb("shell", "input", "text", prompt)
    tap("send-"+prompt, description="发送消息")


def visible(marker, label):
    nodes, record = ui.snapshot(label)
    texts = [node.get("text", "") for node in nodes]
    if not any(marker in text for text in texts):
        return None
    if any('{"reply":' in text for text in texts):
        raise RuntimeError("QA_RAW_MODEL_ENVELOPE_VISIBLE")
    return record


def header_topic(nodes, topic):
    def bounds(node):
        return list(map(int, re.findall(r"\d+", node.get("bounds", ""))))
    anchors = [bounds(node) for node in nodes if node.get("content-desc") == "打开对话历史"]
    for node in nodes:
        box = bounds(node)
        if node.get("text") == topic and len(box) == 4:
            if any(len(anchor) == 4 and anchor[1] <= (box[1]+box[3])/2 <= anchor[3]
                   and box[0] > anchor[0] for anchor in anchors):
                return True
    return False


def installed_apk_hash():
    values = ui.adb("shell", "pm", "path", PACKAGE).decode().splitlines()
    paths = [value.removeprefix("package:") for value in values if value.startswith("package:/data/app/")
             and value.endswith("/base.apk") and ".." not in value]
    if len(paths) != 1 or not re.fullmatch(r"/data/app/[A-Za-z0-9_./=+~-]+/base\.apk", paths[0]):
        raise RuntimeError("QA_INSTALLED_APK_IDENTITY_INVALID")
    digest = ui.adb("shell", "sha256sum", paths[0]).decode().split()[0]
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise RuntimeError("QA_INSTALLED_APK_HASH_UNAVAILABLE")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("Expected a numeric three-part release version")
    directory = ROOT / "artifacts" / ("qa-phone-current-release-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir(parents=True, exist_ok=False)
    ui.OUTPUT = directory
    proof = {"passed": False, "release_version": args.version,
             "physical_device": False, "synthetic_api_only": True,
             "production_keys_used": False, "paired_with_desktop": False,
             "app_data_reset": False, "windows_trust_changed": False}
    stop = threading.Event()
    server_thread = None
    bound = False
    pid = None
    original_roots = None
    stage = "identity"
    qa_store = "/data/local/tmp/aura-qa-current-" + secrets.token_hex(8)
    try:
        if ui.adb("shell", "getprop", "ro.kernel.qemu").strip() != b"1" or b"aura_api35" not in ui.adb("emu", "avd", "name"):
            raise RuntimeError("QA_AVD_IDENTITY_REQUIRED")
        # Do not start or stop an existing listener belonging to somebody else.
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                raise RuntimeError("QA_PORT_ALREADY_IN_USE")
        apk = ROOT / f"android/deliverables/AURA-{args.version}-x86_64.apk"
        verification = json.loads((ROOT / f"artifacts/android/qa/apk-verification-{args.version}.json").read_text(encoding="utf-8"))
        proof["apk_sha256"] = hashlib.sha256(apk.read_bytes()).hexdigest()
        candidates = [row for row in verification.get("packages", [])
                      if Path(row.get("file", "")).name == apk.name]
        if (len(candidates) != 1 or candidates[0].get("sha256") != proof["apk_sha256"]
                or candidates[0].get("release_signature_verified") is not True
                or candidates[0].get("abis") != ["x86_64"]):
            raise RuntimeError("QA_APK_VERIFICATION_MISMATCH")
        proof["installed_apk_sha256"] = installed_apk_hash()
        if proof["installed_apk_sha256"] != proof["apk_sha256"]:
            raise RuntimeError("QA_INSTALLED_APK_CHANGED")
        proof["apk_verification_report_sha256"] = hashlib.sha256(json.dumps(verification, sort_keys=True).encode()).hexdigest()
        ui.adb("shell", "am", "force-stop", PACKAGE)
        stage = "certificates"
        fixture.certificates(directory)
        cert_hash = subprocess.check_output([r"E:\Git\usr\bin\openssl.exe", "x509", "-in", str(directory / "qa-ca.pem"),
                                            "-subject_hash_old", "-noout"], timeout=10).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{8}", cert_hash):
            raise RuntimeError("QA_CA_NAME_INVALID")
        pid = ui.adb("shell", "pidof", "zygote64").decode().strip()
        if not pid.isdigit():
            raise RuntimeError("QA_ZYGOTE_IDENTITY_REQUIRED")
        original_roots = ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "ls", "-1", STORE).splitlines()
        if (cert_hash + ".0").encode() in original_roots:
            raise RuntimeError("QA_CA_NAME_COLLISION")
        mounts = ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/proc/self/mountinfo").decode()
        if any("aura-qa" in line and STORE in line for line in mounts.splitlines()):
            raise RuntimeError("QA_EXISTING_TRUST_OVERLAY_REFUSED")
        stage = "temporary_trust"
        ui.adb("shell", "mkdir", "-p", qa_store)
        ui.adb("shell", "cp", "-a", STORE + "/.", qa_store)
        ui.adb("push", str(directory / "qa-ca.pem"), qa_store + "/" + cert_hash + ".0")
        ui.adb("shell", "chmod", "644", qa_store + "/" + cert_hash + ".0")
        ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "mount", "--bind", qa_store, STORE)
        bound = True
        mounts = ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/proc/self/mountinfo").decode()
        if qa_store.removeprefix("/data") + " " + STORE + " " not in mounts:
            raise RuntimeError("QA_TRUST_OVERLAY_UNCONFIRMED")
        context = ssl.create_default_context(cafile=str(directory / "qa-ca.pem"))
        def request(path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            with urllib.request.urlopen(urllib.request.Request(f"https://127.0.0.1:{PORT}"+path, data=data,
                                        headers={"Content-Type": "application/json"}), context=context, timeout=3) as response:
                return json.load(response)
        server_thread = threading.Thread(target=fixture.serve, args=(directory, PORT, stop), daemon=True)
        server_thread.start()
        stage = "fixture_start"
        wait_for(lambda: request("/_qa/state"), "QA_FIXTURE_NOT_READY")
        ui.adb("shell", "am", "start", "-n", PACKAGE+"/.MainActivity")
        stage = "ui_first_reply"
        def app_ready():
            nodes, _ = ui.snapshot("launch")
            return any(node.get("content-desc") == "打开对话历史" for node in nodes)
        wait_for(app_ready, "QA_APP_NOT_READY")
        tap("more", description="更多选项")
        tap("new-thread", description="新建对话")
        send("QA_FIRST")
        wait_for(lambda: request("/_qa/state")["request_count"] == 1, "QA_FIRST_REQUEST_MISSING")
        first = wait_for(lambda: visible("QA_FIRST_VISIBLE", "first-prefix"), "QA_PREFIX_NOT_VISIBLE")
        state = request("/_qa/state")
        proof["first_prefix_visible_before_terminal"] = not state["requests"][0]["end_sent"]
        proof["prefix_screenshot"] = first["snapshot"] + ".png"
        request("/_qa/release", {"request_id": 1})
        wait_for(lambda: request("/_qa/state")["requests"][0]["end_sent"], "QA_FIRST_NOT_FINISHED")
        wait_for(lambda: visible("回复完整结束", "first-complete"), "QA_COMPLETE_NOT_VISIBLE")
        send("QA_CANCEL")
        stage = "ui_stop"
        wait_for(lambda: visible("QA_CANCEL_PARTIAL", "cancel-prefix"), "QA_CANCEL_PREFIX_NOT_VISIBLE")
        tap("stop", description="停止回复")
        wait_for(lambda: request("/_qa/state")["requests"][1]["disconnected"], "QA_STOP_NOT_DISCONNECTED")
        send("QA_AFTER")
        stage = "ui_after_stop"
        last = wait_for(lambda: visible("QA_AFTER_COMPLETE", "after-stop-complete"), "QA_AFTER_NOT_VISIBLE")
        state = request("/_qa/state")
        proof.update(request_count=state["request_count"], requests=state["requests"],
                     after_stop_complete_visible=True, after_screenshot=last["snapshot"] + ".png")
        nodes, topic = ui.snapshot("topic-persistence")
        proof["first_message_topic_visible"] = header_topic(nodes, "QA_FIRST")
        proof["topic_screenshot"] = topic["snapshot"] + ".png"
        proof["scenario_passed"] = (proof["first_prefix_visible_before_terminal"] and state["request_count"] == 3
            and not state["requests"][1]["end_sent"] and state["requests"][1]["disconnected"]
            and state["requests"][2]["end_sent"] and proof["first_message_topic_visible"])
    except Exception as error:
        # Only a fixed code, never TLS/provider/ADB exception text.
        code = error.args[0] if type(error) is RuntimeError and error.args else "QA_RELEASE_VALIDATION_FAILED"
        proof["error_code"] = code if isinstance(code, str) and re.fullmatch(r"QA_[A-Z0-9_]+", code) else "QA_RELEASE_VALIDATION_FAILED"
        proof["failed_stage"] = stage
    finally:
        try:
            ui.adb("shell", "am", "force-stop", PACKAGE)
            if bound and pid:
                mounts = ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/proc/self/mountinfo").decode()
                if qa_store.removeprefix("/data") + " " + STORE + " " not in mounts:
                    raise RuntimeError("QA_OVERLAY_IDENTITY_CHANGED")
                ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "umount", STORE)
                after_mounts = ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/proc/self/mountinfo").decode()
                after_roots = ui.adb("shell", "nsenter", "-t", pid, "-m", "--", "ls", "-1", STORE).splitlines()
                if qa_store.removeprefix("/data") + " " + STORE + " " in after_mounts or after_roots != original_roots:
                    raise RuntimeError("QA_TRUST_RESTORE_UNCONFIRMED")
            proof["qa_trust_restored"] = True
        except Exception:
            proof["qa_trust_restored"] = False
        stop.set()
        if server_thread:
            server_thread.join(5)
        proof["fixture_stopped"] = server_thread is None or not server_thread.is_alive()
        key = directory / "qa-server.key"
        if key.parent.resolve() == directory.resolve():
            key.unlink(missing_ok=True)
        proof["passed"] = bool(proof.get("scenario_passed") and proof["qa_trust_restored"] and proof["fixture_stopped"])
        (directory / "report.json").write_text(json.dumps(proof, ensure_ascii=True, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"report": str(directory / "report.json"), **proof}, ensure_ascii=True))
    return 0 if proof["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
