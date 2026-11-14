"""Pure helpers for read-only distribution auditing; never read user configuration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def manifest_member(raw: object) -> PurePosixPath:
    """Require canonical manifest paths so hashing cannot escape the candidate."""
    if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw:
        raise ValueError("Non-canonical manifest member")
    if any(ord(character) < 32 for character in raw):
        raise ValueError("Control character in manifest member")
    member = PurePosixPath(raw)
    if member.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ValueError("Manifest member escapes canonical relative namespace")
    if member.as_posix() != raw:
        raise ValueError("Manifest member is not canonical")
    return member


def validate_manifest_rows(rows: object) -> dict[str, dict]:
    if not isinstance(rows, list):
        raise ValueError("Manifest files must be a list")
    records, windows_names = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Manifest file entry must be an object")
        member = manifest_member(row.get("path")).as_posix()
        key = member.casefold()
        if key in windows_names:
            raise ValueError("Duplicate Windows manifest path")
        windows_names.add(key)
        if not isinstance(row.get("bytes"), int) or isinstance(row["bytes"], bool) or row["bytes"] < 0:
            raise ValueError("Manifest length is invalid")
        if not isinstance(row.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None:
            raise ValueError("Manifest checksum is invalid")
        records[member] = row
    return records


def regular_files(root: Path) -> list[Path]:
    """Reject all link/reparse traversal before opening candidate file bytes."""
    result = []
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
                raise ValueError("Candidate contains a link or reparse point")
            if name in files:
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("Candidate contains a non-regular file")
                result.append(path)
    return sorted(result)


TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")
ASSIGNMENT = re.compile(
    r'''(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|bearer)\s*[:=]\s*["'][A-Za-z0-9_./+-]{24,}["']'''
)


def credential_findings(text: str, path: str) -> list[dict]:
    """Return locations and kinds only; never include any matched credential."""
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        if TOKEN.search(line):
            result.append({"path": path, "line": number, "kind": "token-shaped literal"})
        elif ASSIGNMENT.search(line):
            result.append({"path": path, "line": number, "kind": "long literal credential assignment"})
    return result


PRIVATE_NAMES = {
    "settings.ini", "credentials.json", "devices.json", "runtime.json", ".env", ".git-credentials",
    "pyvenv.cfg", "conversation-drafts.json", "conversations.json", "pairing.json", "identity.json",
    "api-config.enc", "api-config.bin", "character-cards.enc", "character_cards.sqlite3",
}
PRIVATE_PARTS = {".git", ".venv", "agent_workspace", "artifacts", "__pycache__"}
PRIVATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".psd", ".mp4", ".pfx", ".p12", ".jks", ".keystore"}


def private_member(relative: Path) -> bool:
    return (relative.name.casefold() in PRIVATE_NAMES
            or relative.name.casefold().startswith(".env.")
            or relative.suffix.casefold() in PRIVATE_SUFFIXES
            or any(part.casefold() in PRIVATE_PARTS for part in relative.parts))
