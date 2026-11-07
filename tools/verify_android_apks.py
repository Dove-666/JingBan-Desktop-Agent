"""Validate the signed Android deliverables without exposing signing secrets."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import xml.etree.ElementTree as ET
from uuid import uuid4
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], environment: dict[str, str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def elf_load_segments(data: bytes) -> list[dict[str, int]]:
    if data[:4] != b"\x7fELF" or data[5] != 1:
        raise ValueError("Expected little-endian ELF native library")
    if data[4] == 2:
        start = struct.unpack_from("<Q", data, 32)[0]
        entry_size, count = struct.unpack_from("<HH", data, 54)
        fields = "<IIQQQQQQ"
        indices = (2, 3, 7)
    elif data[4] == 1:
        start = struct.unpack_from("<I", data, 28)[0]
        entry_size, count = struct.unpack_from("<HH", data, 42)
        fields = "<IIIIIIII"
        indices = (1, 2, 7)
    else:
        raise ValueError("Unsupported ELF class")
    segments = []
    for index in range(count):
        values = struct.unpack_from(fields, data, start + index * entry_size)
        if values[0] == 1:
            offset, virtual_address, alignment = (values[item] for item in indices)
            if alignment < 16384 or (offset - virtual_address) % 16384:
                raise ValueError(f"Native LOAD segment lacks 16 KiB compatibility: {alignment}")
            segments.append({"offset": offset, "virtual_address": virtual_address, "alignment": alignment})
    if not segments:
        raise ValueError("Native library has no LOAD segments")
    return segments


def _release_packages(directory: Path, version: str, product_name: str | None = None):
    """Select one complete product identity, including historical AURA releases."""
    products = (product_name,) if product_name else ("鲸伴", "AURA")
    matches = [(product, sorted(directory.glob(f"{product}-{version}-*.apk"))) for product in products]
    matches = [(product, packages) for product, packages in matches if packages]
    if len(matches) > 1:
        raise RuntimeError("Multiple product names match this release; select --product-name explicitly")
    if len(matches) != 1 or len(matches[0][1]) != 4:
        raise RuntimeError("Expected arm64, ARMv7, x86_64 and universal signed release APKs")
    return matches[0]


def _verify(args: argparse.Namespace) -> dict:
    build_tools = args.sdk / "build-tools/35.0.0"
    environment = dict(os.environ, JAVA_HOME=str(args.jdk))
    report_directory = ROOT / "artifacts/android/qa/apks"
    report_directory.mkdir(parents=True, exist_ok=True)
    reports = []
    product_name, packages = _release_packages(ROOT / "android/deliverables", args.version, args.product_name)
    for package in packages:
        signature = run([str(args.jdk / "bin/java.exe"), "-jar", str(build_tools / "lib/apksigner.jar"),
                         "verify", "--verbose", "--print-certs", str(package)], environment)
        if "Verified using v2 scheme (APK Signature Scheme v2): true" not in signature:
            raise RuntimeError("APK is missing its verified v2 release signature")
        if "Android Debug" in signature:
            raise RuntimeError("A debug certificate cannot be delivered as a release")
        previous_signature = report_directory / "AURA-1.0.0-arm64-v8a-signature.txt"
        same_signer = None
        if args.version != "1.0.0":
            if not previous_signature.is_file():
                raise RuntimeError("Original release signing certificate evidence is missing")
            pattern = r"Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})(?![0-9a-f])"
            previous = re.search(pattern, previous_signature.read_text(encoding="utf-8"))
            current = re.search(pattern, signature)
            same_signer = previous is not None and current is not None and previous.group(1) == current.group(1)
            if not same_signer:
                raise RuntimeError("Update APK must retain the original release signing certificate")
        run([str(build_tools / "zipalign.exe"), "-c", "-P", "16", "4", str(package)], environment)
        badging = run([str(build_tools / "aapt2.exe"), "dump", "badging", str(package)], environment)
        for expected in ("name='com.aura.companion'", f"versionName='{args.version}'", "minSdkVersion:'26'", "targetSdkVersion:'35'"):
            if expected not in badging:
                raise RuntimeError(f"Unexpected release package metadata: {expected}")
        if product_name == "鲸伴" and not re.search(r"^application-label:'鲸伴'\s*$", badging, re.MULTILINE):
            raise RuntimeError("Unexpected application label: expected 鲸伴")
        package_code = re.search(r"^package:.*\bversionCode='([0-9]+)'", badging, re.MULTILINE)
        if args.version_code is not None and (package_code is None or int(package_code.group(1)) != args.version_code):
            raise RuntimeError("Unexpected release package metadata: versionCode")
        if "application-debuggable" in badging:
            raise RuntimeError("Release APK is debuggable")
        native = {}
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            if any(name.endswith((".p12", ".jks", ".dpapi", "aura-e2e-config.json")) for name in names):
                raise RuntimeError("Private build/test artifacts must not enter the APK")
            if any(name.startswith(("darwin-", "linux-", "win32-")) for name in names):
                raise RuntimeError("Desktop-only native libraries unexpectedly entered APK")
            if archive.testzip() is not None:
                raise RuntimeError("APK ZIP integrity verification failed")
            for name in names:
                if name.startswith("lib/") and name.endswith(".so"):
                    native[name] = elf_load_segments(archive.read(name))
            abis = sorted({name.split("/")[1] for name in native})
            expected_abis = ["arm64-v8a", "armeabi-v7a", "x86_64"] if "universal" in package.name else [
                package.stem.removeprefix(f"{product_name}-{args.version}-")]
            if abis != expected_abis:
                raise RuntimeError(f"Unexpected packaged ABIs in {package.name}: {abis}")
            for abi in abis:
                for required in ("libiroh_ffi.so", "libjnidispatch.so"):
                    if f"lib/{abi}/{required}" not in native:
                        raise RuntimeError(f"Missing {required} in {abi}")
        if not native:
            raise RuntimeError("Encrypted transport native libraries are absent")
        (report_directory / f"{package.stem}-signature.txt").write_text(signature, encoding="utf-8")
        (report_directory / f"{package.stem}-metadata.txt").write_text(badging, encoding="utf-8")
        with package.open("rb") as stream:
            checksum = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
            digest = checksum.hexdigest()
        reports.append({"file": str(package.relative_to(ROOT)), "bytes": package.stat().st_size,
                        "sha256": digest,
                        "same_signer_as_1_0_0": same_signer,
                        "abis": abis, "release_signature_verified": True, "zip_alignment_16k": True,
                        "native_load_segments": native})
    unit_tests = []
    for path in sorted((ROOT / "android/app/build/test-results/testDebugUnitTest").glob("TEST-*.xml")):
        suite = ET.parse(path).getroot()
        entry = {name: int(suite.get(name, "0")) for name in ("tests", "failures", "errors", "skipped")}
        entry["suite"] = suite.get("name")
        if entry["failures"] or entry["errors"] or entry["skipped"]:
            raise RuntimeError("Unit tests are incomplete or failing")
        unit_tests.append(entry)
    if not unit_tests:
        raise RuntimeError("Unit test evidence is absent")
    result = {"packages": reports, "unit_tests": unit_tests,
              "unit_tests_total": sum(suite["tests"] for suite in unit_tests)}
    return result


@contextmanager
def _verification_lock(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place. Closing its descriptor releases the OS lock,
    # including after process exit, without letting a second inode bypass it.
    with output.with_suffix(output.suffix + ".lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("APK verification for this version is already running") from error
        yield


def _atomic_json(output: Path, value: dict) -> None:
    temporary = output.with_name("." + output.name + "." + uuid4().hex + ".tmp")
    stream = temporary.open("x", encoding="utf-8")
    try:
        with stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=Path("E:/Android/Sdk"))
    parser.add_argument("--jdk", type=Path, default=Path("E:/Biancheng"))
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--version-code", type=int)
    parser.add_argument("--product-name", choices=("鲸伴", "AURA"),
                        help="Product filename prefix; omitted selects the unique current or historical name")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("Expected a numeric three-part release version")
    if args.version_code is not None and args.version_code < 1:
        parser.error("Expected a positive release version code")
    output = ROOT / "artifacts/android/qa" / ("apk-verification.json" if args.version == "1.0.0" else f"apk-verification-{args.version}.json")
    with _verification_lock(output):
        run_id = uuid4().hex
        previous = None
        if output.exists():
            previous = output.with_name(output.stem + ".previous-" + run_id + ".json")
            try:
                output.rename(previous)
            except OSError as error:
                raise RuntimeError("Cannot start APK verification because the previous report could not be preserved") from error
        attempt = {"status": "running", "run_id": run_id, "version": args.version,
                   "requested_version_code": args.version_code,
                   "previous_report": previous.relative_to(ROOT).as_posix() if previous else None,
                   "packages": [], "unit_tests": [], "unit_tests_total": 0}
        try:
            if previous is not None:
                historical = output.parent / "history" / output.stem / (run_id + ".json")
                historical.parent.mkdir(parents=True, exist_ok=True)
                previous.rename(historical)
                attempt["previous_report"] = historical.relative_to(ROOT).as_posix()
            _atomic_json(output, attempt)
            result = _verify(args)
            _atomic_json(output, {**attempt, **result, "status": "passed"})
        except BaseException as error:
            try:
                _atomic_json(output, {**attempt, "status": "failed",
                                      "error": {"type": type(error).__name__, "message": str(error)}})
            except OSError:
                # The old result is already in history. An absent/running
                # canonical file cannot masquerade as a fresh success.
                pass
            raise
    print(json.dumps({"verified_apks": len(result["packages"]), "unit_tests": result["unit_tests_total"],
                      "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
