"""Upgrade only an explicitly named Android emulator, preserving app data.

Reports hashes, package metadata and booleans; never reads or prints DB content
or decrypted settings. Physical devices are refused by emulator/QEMU checks.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

PACKAGE = "com.aura.companion"


def verify(adb, serial, apk):
    if not re.fullmatch(r"emulator-[0-9]{4,5}", serial):
        raise ValueError("EMULATOR_REQUIRED")
    def call(*args, check=True):
        result = subprocess.run([str(adb), "-s", serial, *args], capture_output=True, timeout=60)
        if check and result.returncode:
            raise RuntimeError("QA_ADB_OPERATION_FAILED")
        return result.stdout.decode("utf-8", errors="replace").strip()
    if call("shell", "getprop", "ro.kernel.qemu") != "1":
        raise ValueError("QEMU_EMULATOR_REQUIRED")
    if call("shell", "su", "0", "id").find("uid=0") < 0:
        raise ValueError("QA_HASH_ACCESS_UNAVAILABLE")
    call("shell", "am", "force-stop", PACKAGE)
    def metadata():
        data = call("shell", "dumpsys", "package", PACKAGE)
        fields = {}
        for key in ("versionCode", "versionName", "firstInstallTime"):
            match = re.search(re.escape(key) + r"=([^\r\n]+)", data)
            fields[key] = match.group(1).strip() if match else None
        return fields
    def hashes():
        base = "/data/user/0/" + PACKAGE
        listing = call("shell", "su", "0", "find", base + "/databases", base + "/shared_prefs", "-type", "f", check=False)
        values = {}
        for name in listing.splitlines():
            if not name.startswith((base + "/databases/", base + "/shared_prefs/")) or ".." in name:
                continue
            raw = call("shell", "su", "0", "sha256sum", name)
            digest = raw.split()[0] if raw.split() else ""
            if re.fullmatch(r"[a-f0-9]{64}", digest):
                values[name.removeprefix(base + "/")] = digest
        return values
    before, before_hashes = metadata(), hashes()
    installed = call("install", "-r", str(apk.resolve()))
    if "Success" not in installed:
        raise RuntimeError("QA_INSTALL_FAILED")
    after, after_hashes = metadata(), hashes()
    return {"emulator": serial, "package": PACKAGE, "before": before, "after": after,
            "files_checked": len(before_hashes), "userdata_hashes_unchanged": before_hashes == after_hashes,
            "first_install_time_preserved": before["firstInstallTime"] == after["firstInstallTime"],
            "apk_sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
            "data_cleared": False, "uninstalled": False,
            "scope": "Package replacement and unchanged existing encrypted preferences/database bytes before first launch; no paired-device-data claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--adb", type=Path, default=Path("E:/Android/Sdk/platform-tools/adb.exe"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.adb, args.serial, args.apk)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["userdata_hashes_unchanged"] and report["first_install_time_preserved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
