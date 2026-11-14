"""Read a finished candidate only; check bytes, privacy filters and product branding."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import types

from audit_helpers import credential_findings, private_member, regular_files, sha256, validate_manifest_rows

ROOT = Path(__file__).resolve().parents[2]


def literal_assignments(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = value
    return result


def code_strings(code):
    if isinstance(code, types.CodeType):
        for value in code.co_consts:
            if isinstance(value, str):
                yield value
            elif isinstance(value, types.CodeType):
                yield from code_strings(value)


def product_version(path):
    import pefile
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        fixed = pe.VS_FIXEDFILEINFO[0]
        strings = {}
        for group in pe.FileInfo:
            for item in group:
                for table in getattr(item, "StringTable", []):
                    strings.update({key.decode("utf-8"): value.decode("utf-8") for key, value in table.entries.items()})
        return {"file_tuple": [fixed.FileVersionMS >> 16, fixed.FileVersionMS & 0xffff,
                                fixed.FileVersionLS >> 16, fixed.FileVersionLS & 0xffff],
                "product_tuple": [fixed.ProductVersionMS >> 16, fixed.ProductVersionMS & 0xffff,
                                   fixed.ProductVersionLS >> 16, fixed.ProductVersionLS & 0xffff],
                "flags": fixed.FileFlags, "strings": strings}
    finally:
        pe.close()


def check_installer_source(path):
    text = path.read_text(encoding="utf-8-sig")
    properties = {}
    in_setup = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_setup = line == "[Setup]"
        elif in_setup and "=" in line and not line.startswith(";"):
            key, value = line.split("=", 1)
            properties[key] = value
    expected = {"AppName": "鲸伴", "AppVerName": "鲸伴", "AppPublisher": "鲸伴",
                "OutputBaseFilename": "鲸伴安装", "UninstallDisplayName": "鲸伴",
                "VersionInfoProductName": "鲸伴", "VersionInfoVersion": "{#AppVersion}.0",
                "VersionInfoProductVersion": "{#AppVersion}.0"}
    assert all(properties.get(key) == value for key, value in expected.items()), "Installer brand/version differs"
    assert not any(key in properties for key in ("InfoBeforeFile", "InfoAfterFile")), "Installer has extra information page"
    return {"neutral_product_name": True, "information_pages_absent_in_source": True,
            "output_name": properties["OutputBaseFilename"] + ".exe", "source_sha256": sha256(path)}


def audit(payload):
    from PyInstaller.archive.readers import CArchiveReader
    expected_version = literal_assignments(ROOT / "desktop_pet/__init__.py")["APP_VERSION"]
    assert payload.name == "鲸伴", "Portable root must have the product name only"
    manifest_path = payload / "PACKAGE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["product"] == "鲸伴" and manifest["version"] == expected_version
    assert manifest["developer_configuration_included"] is False
    rows = validate_manifest_rows(manifest["files"])
    files = regular_files(payload)
    actual_paths = {path.relative_to(payload).as_posix() for path in files}
    assert actual_paths == set(rows) | {"PACKAGE-MANIFEST.json"}, "Manifest misses or adds candidate files"
    assert manifest["file_count"] == len(rows)
    assert manifest["bytes"] == sum(row["bytes"] for row in rows.values())
    private = [path.relative_to(payload).as_posix() for path in files if private_member(path.relative_to(payload))]
    assert not private, "Candidate contains prohibited private/development state"
    for name, row in rows.items():
        path = payload / name
        assert path.stat().st_size == row["bytes"] and sha256(path) == row["sha256"], name
    assert not any((payload / name).exists() for name in ("首次使用必读.txt", "VERSION.txt")), "Obsolete visible release files"
    guide = (payload / "使用说明.txt").read_text(encoding="utf-8-sig")
    assert guide.splitlines()[0] == "鲸伴"
    assert not re.search(r"测试版|正式版|体验版|公开测试|beta\.?\d", guide, re.I), "Guide contains release-channel marketing labels"
    assert expected_version not in guide, "Version should appear in Settings, not the guide heading"
    identity = literal_assignments(ROOT / "desktop_pet/__init__.py")
    assert identity["APP_VERSION"] == expected_version
    version = product_version(payload / "鲸伴.exe")
    expected_tuple = [int(part) for part in expected_version.split(".")] + [0]
    assert version["file_tuple"] == expected_tuple and version["product_tuple"] == expected_tuple
    assert version["flags"] == 0, "Executable still carries debug/prerelease version flags"
    for key, expected in {"FileDescription": "鲸伴", "ProductName": "鲸伴", "CompanyName": "鲸伴",
                          "ProductVersion": expected_version, "OriginalFilename": "鲸伴.exe"}.items():
        assert version["strings"].get(key) == expected, key
    credential_rows, scanned, scanned_code_objects = [], [], 0
    project_text = []
    for path in files:
        relative = path.relative_to(payload).as_posix()
        if (relative.startswith(("_internal/desktop_pet/", "_internal/agent_backend/", "browser-extension/", "tools/"))
                and "/node_modules/" not in relative and path.suffix in {".py", ".js", ".mjs", ".json", ".html", ".ps1", ".txt", ".css"}):
            project_text.append(path)
    for path in project_text:
        relative = path.relative_to(payload).as_posix()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            continue
        credential_rows.extend(credential_findings(text, relative))
        scanned.append(relative)
    archive = CArchiveReader(str(payload / "鲸伴.exe"))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    for module in pyz.toc:
        if module == "desktop_pet" or module.startswith("desktop_pet."):
            scanned_code_objects += 1
            for text in code_strings(pyz.extract(module)):
                credential_rows.extend(credential_findings(text, "embedded:" + module))
    # Report locations and categories only; a credential value is never serialized.
    assert not credential_rows, "Credential-shaped literal found; inspect local source without printing its value"
    installer = check_installer_source(ROOT / "tools/packaging/jingban.iss")
    before_manifest = sha256(manifest_path)
    return {"passed": True, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "payload": str(payload), "version": expected_version, "files_hashed": len(rows),
            "payload_bytes": manifest["bytes"], "manifest_sha256": before_manifest,
            "prohibited_state_files": private, "credential_findings": credential_rows,
            "project_text_files_scanned": len(scanned), "embedded_project_modules_scanned": scanned_code_objects,
            "pe_version": version, "installer_source": installer, "guide_neutral": True,
            "all_manifest_file_bytes_matched": True,
            "scope": "Finished candidate file names/full SHA256 manifest; project text and embedded first-party string constants; main PE version and neutral source installer properties. This cannot compare against undisclosed developer secrets and does not execute the installer UI or application.",
            "real_provider_calls": 0, "production_configuration_accessed": False, "application_launched": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.tag):
        parser.error("Use a simple unique audit tag")
    payload = args.payload.resolve()
    if not (payload / "鲸伴.exe").is_file():
        parser.error("Candidate executable must already exist; no build or launch is performed")
    out = ROOT / "artifacts/release-validation" / args.tag
    out.mkdir(parents=True, exist_ok=False)
    try:
        report = audit(payload)
    except BaseException as error:
        # Assertions above use fixed messages/path-only labels; no matched secret is printed.
        report = {"passed": False, "error_type": type(error).__name__, "error": str(error),
                  "real_provider_calls": 0, "production_configuration_accessed": False,
                  "application_launched": False}
        (out / "payload.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))
        return 1
    report["audit_source_sha256"] = {path.name: sha256(path) for path in Path(__file__).parent.glob("*.py")}
    (out / "payload.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("passed", "files_hashed", "project_text_files_scanned", "embedded_project_modules_scanned", "real_provider_calls")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
