"""Read-only candidate comparison with locally held private values. No network calls."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.qa_release.audit_helpers import regular_files, validate_manifest_rows

CHUNK_BYTES = 1024 * 1024
INSTALLER_EXTRA_FILES = frozenset({"unins000.exe", "unins000.dat", "unins000.msg"})
PYINSTALLER_EXECUTABLES = frozenset({"鲸伴.exe", "鲸伴网页桥.exe"})


def forms(value: str, *, path: bool = False) -> set[str]:
    variants = {value}
    if path:
        variants.update((value.replace("\\", "/"), value.replace("/", "\\")))
    for item in tuple(variants):
        variants.add(json.dumps(item, ensure_ascii=False)[1:-1])
        variants.add(json.dumps(item, ensure_ascii=True)[1:-1])
        if path:
            variants.add(item.replace("/", "\\/"))
    return {item for item in variants if item}


class Literals:
    """Private needles never appear in repr, errors, reports or command arguments."""
    def __init__(self, profiles=(), keys=()):
        self.patterns = {"profile_path": set(), "api_key": set()}
        self.labels = []
        for kind, values in (("profile_path", profiles), ("api_key", keys)):
            for value in values:
                for variant in forms(value, path=kind == "profile_path"):
                    self.labels.append(variant)
                    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                        encoded = variant.encode(encoding)
                        self.patterns[kind].add(encoded.lower() if kind == "profile_path" else encoded)
        self.overlap = max((len(item) for values in self.patterns.values() for item in values), default=1) - 1

    def safe_label(self, value):
        # Filenames themselves may contain a private literal.
        for private in sorted(self.labels, key=len, reverse=True):
            value = value.replace(private, "[redacted]")
        return value

    def scan(self, stream, *, chunk_bytes=CHUNK_BYTES):
        counts = {kind: 0 for kind in self.patterns}
        tail, size = b"", 0
        while chunk := stream.read(chunk_bytes):
            size += len(chunk)
            buffer = tail + chunk
            folded = buffer.lower()
            for kind, needles in self.patterns.items():
                source = folded if kind == "profile_path" else buffer
                for needle in needles:
                    # Ignore matches wholly contained in the previous chunk's tail.
                    start = max(0, len(tail) - len(needle) + 1)
                    counts[kind] += source.count(needle, start)
            tail = buffer[-self.overlap:] if self.overlap else b""
        return {kind: count for kind, count in counts.items() if count}, size


def private_inputs():
    """Only getters run. Do not call load/save/migration methods or model clients."""
    from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths
    from desktop_pet.settings_manager import SettingsManager, CredentialStore

    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    override = os.environ.get("JINGBAN_PROFILE_DIR")
    profile = Path(override).expanduser().resolve() if override else Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
    setting_path = profile / "settings.ini"
    profiles = {str(Path.home()), str(profile)}
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        if os.environ.get(name): profiles.add(str(Path(os.environ[name])))
    # Supplying an existing QSettings object avoids SettingsManager's mkdir path.
    store = None
    if override:
        scope = hashlib.sha256(os.path.normcase(str(profile)).encode("utf-8")).hexdigest()[:24]
        store = CredentialStore(service="AURA Desktop Agent · profile-" + scope)
    before = hashlib.sha256(setting_path.read_bytes()).digest() if setting_path.is_file() else None
    manager = SettingsManager(QSettings(str(setting_path), QSettings.Format.IniFormat), store)
    keys, failures, current = set(), 0, "not_configured"
    if not manager.credentials_available:
        failures += 1
        current = "unavailable"
    else:
        for index, getter in enumerate((manager.get_api_key, manager.get_agent_gui_api_key)):
            try:
                value = getter()
                if value: keys.add(value)
                if index == 0: current = "configured" if value else "not_configured"
            except BaseException:
                # Vault exceptions can contain credentials. Never retain their text.
                failures += 1
                if index == 0: current = "unavailable"
    after = hashlib.sha256(setting_path.read_bytes()).digest() if setting_path.is_file() else None
    if before != after: raise RuntimeError("CONFIGURATION_CHANGED")
    status = {"current_key": current, "configured_keys": len(keys), "unreadable_stores": failures,
              "comparison": "unavailable" if failures else "enabled" if keys else "skipped_not_configured"}
    return Literals(profiles, keys), status


def code_values(code):
    if isinstance(code, (str, bytes)):
        yield code
    elif isinstance(code, types.CodeType):
        yield code.co_filename
        for value in code.co_consts:
            yield from code_values(value)
    elif isinstance(code, (tuple, frozenset)):
        for value in code:
            yield from code_values(value)


def audit(payload: Path, literals: Literals, key_status: dict):
    manifest_path = payload / "PACKAGE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    rows = validate_manifest_rows(manifest.get("files"))
    files = regular_files(payload)
    names = {path.relative_to(payload).as_posix() for path in files}
    expected = set(rows) | {"PACKAGE-MANIFEST.json"}
    extras = names - expected
    # Inno Setup adds only these root-level uninstall artifacts. They still go
    # through the raw-byte scan; unrelated/unregistered files remain an error.
    if expected - names or extras - INSTALLER_EXTRA_FILES:
        raise ValueError("CANDIDATE_INVENTORY_MISMATCH")
    findings, total, embedded_count = [], 0, 0
    for path in files:
        with path.open("rb") as stream:
            counts, size = literals.scan(stream)
        total += size
        name = path.relative_to(payload).as_posix()
        findings.extend({"path": literals.safe_label(name), "kind": kind, "count": count}
                        for kind, count in counts.items())
    # Python code constants are compressed inside the executable. Scan those
    # separately so the raw-byte pass cannot miss an embedded account literal.
    from PyInstaller.archive.readers import CArchiveReader
    for path in files:
        if path.parent != payload or path.name not in PYINSTALLER_EXECUTABLES:
            continue
        archive = CArchiveReader(str(path))
        if "PYZ.pyz" not in archive.toc: continue
        pyz = archive.open_embedded_archive("PYZ.pyz")
        for module in pyz.toc:
            code = pyz.extract(module)
            if not isinstance(code, types.CodeType): continue
            embedded_count += 1
            totals = {}
            for value in code_values(code):
                raw = value.encode("utf-8") if isinstance(value, str) else value
                counts, _ = literals.scan(io.BytesIO(raw))
                for kind, count in counts.items(): totals[kind] = totals.get(kind, 0) + count
            name = f"{path.name}:embedded:{module}"
            findings.extend({"path": literals.safe_label(name), "kind": kind, "count": count}
                            for kind, count in totals.items())
    return {"passed": not findings and not key_status["unreadable_stores"],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(), "files_scanned": len(files),
            "bytes_scanned": total, "embedded_modules_scanned": embedded_count,
            "installer_extra_files": sorted(extras),
            "api_key_comparison": key_status, "findings": findings,
            "match_count": sum(item["count"] for item in findings),
            "scope": "All candidate raw bytes in UTF-8/UTF-16 plus slash/JSON variants; PyInstaller embedded code constants. Other compressed archive contents are not decoded. Counts are matching literal representations, not unique secrets.",
            "network_calls": 0, "production_configuration_modified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    # A sink discards dependency diagnostics immediately, even if an exception
    # includes a credential. Never use an in-memory log that might be serialized.
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            payload, report_path = args.payload.resolve(), args.report.resolve()
            if (payload == ROOT or payload.is_relative_to(ROOT) or not (payload / "PACKAGE-MANIFEST.json").is_file()
                    or not (payload / "鲸伴.exe").is_file() or report_path.is_relative_to(payload)):
                raise ValueError("INDEPENDENT_CANDIDATE_REQUIRED")
            literals, key_status = private_inputs()
            report = audit(payload, literals, key_status)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        except BaseException:
            report = {"passed": False, "code": "PRIVATE_LITERAL_AUDIT_FAILED", "network_calls": 0,
                      "production_configuration_modified": False}
            # Do not overwrite an existing report or expose any exception body.
            try:
                report_path = args.report.resolve()
                if not report_path.exists() and not report_path.is_relative_to(args.payload.resolve()):
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            except BaseException:
                pass
    print(json.dumps({key: report[key] for key in ("passed", "network_calls")}, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
