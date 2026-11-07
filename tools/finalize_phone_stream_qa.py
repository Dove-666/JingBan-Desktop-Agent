"""Finalize synthetic release SSE evidence and restore only the QA AVD CA bind."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "artifacts/qa-phone-release-stream"
ADB = "E:/Android/Sdk/platform-tools/adb.exe"


def adb(*args, required=True):
    result = subprocess.run([ADB, "-s", "emulator-5560", *args], capture_output=True, timeout=30)
    if required and result.returncode:
        raise RuntimeError("QA_ADB_FAILED")
    return result


def main():
    state = json.loads((DIRECTORY / "resumed-after-state.json").read_text())
    xml = max(DIRECTORY.glob("*resumed-after-complete.xml"), key=lambda p: p.stat().st_mtime)
    texts = [node.get("text", "") for node in ET.parse(xml).iter("node")]
    proof = {"version": 1, "emulator": "emulator-5560", "release_version": "1.11.0",
             "first_prefix": json.loads((DIRECTORY / "first-prefix-proof.json").read_text()),
             "requests": state["requests"], "request_count": state["request_count"],
             "after_cancel_complete_visible": any("QA_AFTER_COMPLETE" in value for value in texts),
             "raw_envelope_visible": any('{"reply":' in value for value in texts),
             "synthetic_api_only": True, "production_keys_used": False, "paired_with_desktop": False}
    if b"aura_api35" not in adb("emu", "avd", "name").stdout:
        raise RuntimeError("QA_AVD_IDENTITY_REQUIRED")
    pid = adb("shell", "pidof", "zygote64").stdout.decode().strip()
    if not pid.isdigit():
        raise RuntimeError("QA_ZYGOTE_IDENTITY_REQUIRED")
    mounts = adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/proc/self/mountinfo").stdout.decode()
    bound = "/local/tmp/aura-qa-cacerts-20261102-1547 /apex/com.android.conscrypt/cacerts " in mounts
    adb("shell", "am", "force-stop", "com.aura.companion")
    if bound:
        adb("shell", "nsenter", "-t", pid, "-m", "--", "umount", "/apex/com.android.conscrypt/cacerts")
    after = adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/proc/self/mountinfo").stdout.decode()
    proof["qa_ca_bind_removed"] = "/local/tmp/aura-qa-cacerts-20261102-1547 /apex/com.android.conscrypt/cacerts " not in after
    cert = adb("shell", "nsenter", "-t", pid, "-m", "--", "cat", "/apex/com.android.conscrypt/cacerts/d42aa978.0", required=False)
    proof["qa_ca_not_in_restored_store"] = (cert.returncode != 0 or
        hashlib.sha256(cert.stdout).digest() != hashlib.sha256((DIRECTORY / "qa-ca.pem").read_bytes()).digest())
    proof["passed"] = (proof["request_count"] == 3 and proof["after_cancel_complete_visible"]
        and not proof["raw_envelope_visible"] and state["requests"][1]["disconnected"]
        and not state["requests"][1]["end_sent"] and state["requests"][2]["end_sent"]
        and proof["qa_ca_bind_removed"] and proof["qa_ca_not_in_restored_store"])
    (DIRECTORY / "release-validation.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: proof[key] for key in ("passed", "request_count", "after_cancel_complete_visible",
        "qa_ca_bind_removed", "qa_ca_not_in_restored_store")}))
    return 0 if proof["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
