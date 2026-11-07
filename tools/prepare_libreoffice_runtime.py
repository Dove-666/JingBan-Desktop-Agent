"""Extract a pinned vendor MSI as data; never execute installation actions.

No MSI install/admin transaction, registry changes, file associations or user
Office profile is used. Windows Installer database APIs are read-only; only the
Microsoft cabinet expander writes into a new explicit E-drive runtime folder.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

VERSION = "26.8.2"
PACKAGE_BYTES = 375934976
PACKAGE_SHA256 = "ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a"
VENDOR_METADATA = "https://download.documentfoundation.org/libreoffice/stable/26.8.2/win/x86_64/LibreOffice_26.8.2_Win_x86-64.msi.mirrorlist"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""): value.update(block)
    return value.hexdigest()


def verify_package(path):
    if not path.is_file() or path.stat().st_size != PACKAGE_BYTES or digest(path) != PACKAGE_SHA256:
        raise ValueError("LIBREOFFICE_VENDOR_PACKAGE_MISMATCH")
    quoted = str(path).replace("'", "''")
    command = "$s=Get-AuthenticodeSignature -LiteralPath '" + quoted + "'; " + \
        "[pscustomobject]@{status=[string]$s.Status;signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    import base64
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile",
        "-NonInteractive", "-EncodedCommand", encoded], capture_output=True, text=True, timeout=30,
        creationflags=0x08000000, env={"SystemRoot": r"C:\Windows", "PSModulePath":
            r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"})
    identity = json.loads(result.stdout) if result.returncode == 0 else {}
    if identity.get("status") != "Valid" or "CN=The Document Foundation," not in identity.get("signer", ""):
        raise ValueError("LIBREOFFICE_VENDOR_SIGNATURE_UNCONFIRMED")
    return identity


def msi_rows(database, query):
    view = database.OpenView(query)
    try:
        view.Execute(None)
        while True:
            record = view.Fetch()
            if record is None: break
            yield [record.GetString(index) for index in range(1, record.GetFieldCount() + 1)]
    finally: view.Close()


def extract_cabinet(package, output):
    """MsiRecordReadStream only: no action/installation entrypoint is called."""
    native = C.WinDLL(r"C:\Windows\System32\msi.dll")
    signatures = {
        "MsiOpenDatabaseW": ([C.c_wchar_p, C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
        "MsiDatabaseOpenViewW": ([C.c_uint32, C.c_wchar_p, C.POINTER(C.c_uint32)], C.c_uint32),
        "MsiViewExecute": ([C.c_uint32, C.c_uint32], C.c_uint32),
        "MsiViewFetch": ([C.c_uint32, C.POINTER(C.c_uint32)], C.c_uint32),
        "MsiRecordReadStream": ([C.c_uint32, C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
        "MsiCloseHandle": ([C.c_uint32], C.c_uint32),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(native, name); method.argtypes, method.restype = arguments, result
    database, view, record = C.c_uint32(), C.c_uint32(), C.c_uint32()
    def ok(result):
        if result != 0: raise ValueError("LIBREOFFICE_MSI_READ_FAILED")
    try:
        ok(native.MsiOpenDatabaseW(str(package), None, C.byref(database)))  # MSIDBOPEN_READONLY=0
        ok(native.MsiDatabaseOpenViewW(database, "SELECT `Data` FROM `_Streams` WHERE `Name`='libreoffice1.cab'", C.byref(view)))
        ok(native.MsiViewExecute(view, 0)); ok(native.MsiViewFetch(view, C.byref(record)))
        total = 0
        with output.open("xb") as stream:
            while True:
                buffer, count = C.create_string_buffer(1048576), C.c_uint32(1048576)
                ok(native.MsiRecordReadStream(record, 1, buffer, C.byref(count)))
                if count.value == 0: break
                total += count.value
                if total > PACKAGE_BYTES: raise ValueError("LIBREOFFICE_CABINET_LIMIT")
                stream.write(buffer.raw[:count.value])
    finally:
        for handle in (record.value, view.value, database.value):
            if handle: native.MsiCloseHandle(handle)


def installation_plan(database):
    directories = {key: (parent, name) for key, parent, name in msi_rows(database,
        "SELECT `Directory`,`Directory_Parent`,`DefaultDir` FROM `Directory`")}
    components = dict(msi_rows(database, "SELECT `Component`,`Directory_` FROM `Component`"))
    def component_path(key, seen=()):
        if key == "INSTALLLOCATION": return Path()
        if key in seen or len(seen) > 40: raise ValueError("LIBREOFFICE_DIRECTORY_INVALID")
        parent, value = directories[key]
        if not parent: return None
        prefix = component_path(parent, (*seen, key))
        if prefix is None: return None
        name = value.split(":", 1)[0].split("|")[-1]
        if name == ".": return prefix
        if not name or name in {"..", "."} or any(char in name for char in "/\\:\x00"):
            raise ValueError("LIBREOFFICE_DIRECTORY_INVALID")
        return prefix / name
    plan = []
    for identifier, component, name, size in msi_rows(database,
            "SELECT `File`,`Component_`,`FileName`,`FileSize` FROM `File`"):
        if not identifier or any(char in identifier for char in "/\\:\x00") or identifier in {".", ".."}:
            raise ValueError("LIBREOFFICE_CABINET_NAME_INVALID")
        relative = component_path(components[component])
        if relative is None: continue
        name = name.split("|")[-1]
        if not name or name in {".", ".."} or any(char in name for char in "/\\:\x00"):
            raise ValueError("LIBREOFFICE_FILE_NAME_INVALID")
        plan.append((identifier, relative / name, int(size)))
    if len(plan) > 25000 or sum(item[2] for item in plan) > 3 * 1024**3:
        raise ValueError("LIBREOFFICE_RUNTIME_LIMIT")
    destinations = [str(item[1]).casefold() for item in plan]
    if len(destinations) != len(set(destinations)): raise ValueError("LIBREOFFICE_DUPLICATE_TARGET")
    return plan


def prepare(package, target):
    if os.name != "nt": raise ValueError("LIBREOFFICE_WINDOWS_REQUIRED")
    package, target = package.resolve(strict=True), target.absolute()
    if target.drive.casefold() != "e:": raise ValueError("LIBREOFFICE_E_DRIVE_REQUIRED")
    if target.exists(): raise ValueError("LIBREOFFICE_NEW_TARGET_REQUIRED")
    signature = verify_package(package)
    import _msi
    database = _msi.OpenDatabase(str(package), _msi.MSIDBOPEN_READONLY)
    media = list(msi_rows(database, "SELECT `Cabinet` FROM `Media`"))
    if media != [["#libreoffice1.cab"]]: raise ValueError("LIBREOFFICE_UNEXPECTED_CABINET")
    plan = installation_plan(database)
    target.mkdir(parents=True)
    unpack = target / "cabinet-data"; unpack.mkdir()
    cabinet = target / "libreoffice1.cab"
    extract_cabinet(package, cabinet)
    result = subprocess.run([r"C:\Windows\System32\expand.exe", "-F:*", str(cabinet), str(unpack)],
        capture_output=True, timeout=120, creationflags=0x08000000)
    if result.returncode: raise ValueError("LIBREOFFICE_CABINET_EXTRACT_FAILED")
    runtime = target / "runtime"; runtime.mkdir()
    files = []
    for identifier, relative, size in plan:
        source, destination = unpack / identifier, runtime / relative
        if not source.is_file() or source.is_symlink() or source.stat().st_size != size:
            raise ValueError("LIBREOFFICE_CABINET_CONTENT_MISMATCH")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        files.append({"file": relative.as_posix(), "bytes": size, "sha256": digest(destination)})
    receipt = {"schema": "aura.libreoffice.extracted_runtime.v1", "version": VERSION,
        "vendor_metadata": VENDOR_METADATA, "package_sha256": PACKAGE_SHA256, "signature": signature,
        "installation_actions_executed": False, "user_profile_used": False,
        "file_count": len(files), "runtime_bytes": sum(item["bytes"] for item in files), "files": files}
    (target / "runtime-manifest.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return {key: value for key, value in receipt.items() if key != "files"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(prepare(options.package, options.target)))
