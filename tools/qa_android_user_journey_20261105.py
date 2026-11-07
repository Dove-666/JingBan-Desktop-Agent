"""Owned, offline Android UI acceptance. Never launches/updates the release package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/user-ux-20261105/android"
ADB = Path("E:/Android/Sdk/platform-tools/adb.exe")
SERIAL = "emulator-5560"
DEBUG = "com.aura.companion.debug"
RELEASE = "com.aura.companion"
EXPECTED_DEBUG_CERT = "18ebee424a818bd717a191b57655ee6ca47a87434b299e7583bcee3d046b0493"
SOURCES = [
    "android/app/src/main/java/com/aura/companion/ui/AuraApp.kt",
    "android/app/src/main/java/com/aura/companion/ui/ConversationDrafts.kt",
    "android/app/src/test/java/com/aura/companion/ui/ConversationDraftsTest.kt",
    "android/app/src/androidTest/java/com/aura/companion/ui/UserJourneyEdgeUiTest.kt",
    "android/app/src/main/java/com/aura/companion/data/DirectApi.kt",
    "android/app/src/test/java/com/aura/companion/data/DirectImageContextTest.kt",
]
SCREENSHOTS = ["conversation-draft-return-a.png", "oversized-paste.png",
               "narrow-large-type-artifact.png", "narrow-large-type-running-stop.png"]


def adb(*args: str, timeout: int = 60) -> bytes:
    return subprocess.run([str(ADB), "-s", SERIAL, *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout).stdout


def device_guard() -> None:
    assert adb("emu", "avd", "name").decode().splitlines()[0].strip() == "aura_api35"
    assert adb("shell", "getprop", "sys.boot_completed").strip() == b"1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources() -> dict[str, str]:
    return {name: sha(ROOT / name) for name in SOURCES}


def release_state() -> dict:
    raw = adb("shell", "dumpsys", "package", RELEASE).decode()
    metadata = {}
    for key in ("versionCode", "versionName", "lastUpdateTime", "firstInstallTime"):
        metadata[key] = re.search(r"^\s*" + key + r"=(.*)$", raw, re.M).group(1).strip()
    directory = f"/data/user/0/{RELEASE}/"
    files = adb("shell", "su", "0", "find", directory + "databases", directory + "shared_prefs", "-type", "f").decode().splitlines()
    hashes = {}
    for name in files:
        assert name.startswith(directory) and re.fullmatch(r"[A-Za-z0-9_./-]+", name)
        digest = adb("shell", "su", "0", "sha256sum", name).decode().split()[0]
        hashes[name[len(directory):]] = digest
    return {"release_package_metadata": metadata, "release_file_hashes": hashes,
            "wm_size": adb("shell", "wm", "size").decode().strip(),
            "wm_density": adb("shell", "wm", "density").decode().strip(),
            "font_scale": adb("shell", "settings", "get", "system", "font_scale").decode().strip()}


def compare_release(state: dict) -> None:
    baseline = json.loads((OUT / "device-baseline.json").read_text(encoding="utf-8-sig"))
    for key, value in state.items():
        assert baseline[key] == value, f"Release/device preservation mismatch: {key}"


def capture(run: str, remote_run: str | None = None) -> None:
    destination = OUT / run
    destination.mkdir(exist_ok=True)
    for name in SCREENSHOTS:
        suffix = f"{remote_run}/" if remote_run else ""
        png = adb("exec-out", "run-as", DEBUG, "cat", f"files/qa/user-ux-20261105/{suffix}{name}")
        assert png.startswith(b"\x89PNG\r\n\x1a\n"), name
        (destination / name).write_bytes(png)


def install_debug() -> dict[str, str]:
    apks = [ROOT / "android/app/build/outputs/apk/debug/app-x86_64-debug.apk",
            ROOT / "android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"]
    env = os.environ.copy()
    env["JAVA_HOME"] = "E:/Biancheng"
    for apk in apks:
        certificate = subprocess.check_output(["E:/Android/Sdk/build-tools/35.0.0/apksigner.bat",
            "verify", "--print-certs", str(apk)], env=env, text=True)
        actual = re.search(r"Signer #1 certificate SHA-256 digest: (\w+)", certificate).group(1)
        assert actual == EXPECTED_DEBUG_CERT, apk.name
        # Debug and its synthetic test APK only; no clear/uninstall or release APK.
        assert "debug" in apk.name.lower()
        assert "Success" in adb("install", "-r", str(apk), timeout=120).decode()
    return {str(apk.relative_to(ROOT)): sha(apk) for apk in apks}


def run_after(run: str) -> None:
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", run)
    device_guard()
    compare_release(release_state())
    before = sources()
    apks = install_debug()
    log = adb("shell", "am", "instrument", "-w", "-r", "-e", "class",
        "com.aura.companion.ui.UserJourneyEdgeUiTest", "-e", "qa_output", run,
        DEBUG + ".test/androidx.test.runner.AndroidJUnitRunner", timeout=240).decode()
    (OUT / f"{run}-instrumentation.log").write_text(log, encoding="utf-8")
    after = sources()
    state = release_state()
    compare_release(state)
    report = {"source_before": before, "source_after": after, "sources_stable": before == after,
        "apks": apks, "release_device_preserved": True, "release_after": state,
        "real_model_requests": 0, "passed": "OK (" in log and "FAILURES!!!" not in log,
        "instrumentation_tail": log[-1800:]}
    (OUT / f"{run}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    capture(run, run)
    print(json.dumps({key: report[key] for key in ("sources_stable", "release_device_preserved", "real_model_requests", "passed", "instrumentation_tail")}, ensure_ascii=False))
    assert report["sources_stable"] and report["passed"], "See preserved instrumentation log."


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["capture-before", "run-after", "verify-release"])
    parser.add_argument("--run", default="after")
    args = parser.parse_args()
    device_guard()
    if args.action == "capture-before":
        capture("corrected-fixture-before-fix")
        print("Preserved corrected-fixture failure screenshots.")
    elif args.action == "run-after":
        run_after(args.run)
    else:
        compare_release(release_state())
        print("Release package, private-file hashes and device settings match baseline.")
