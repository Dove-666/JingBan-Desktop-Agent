"""Immutable, conversation-bound delivery copies, never arbitrary remote paths."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from .identities import conversation_uuid

_REFERENCE = re.compile(r"artifact_[a-f0-9]{64}\Z")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
# Windows file associations can report a private or missing ZIP MIME. Keep
# verified archive metadata portable across desktop and Android consumers.
_FIXED_MIME = {".zip": "application/zip"}


def checked_artifact_metadata(value, reference):
    """Validate the host snapshot envelope before any destination is changed."""
    keys = ("reference", "name", "mime", "bytes", "sha256", "verification_scope")
    if not isinstance(value, dict) or any(key not in value for key in keys):
        raise ValueError("ARTIFACT_COPY_CORRUPTED")
    name, mime, size = value["name"], value["mime"], value["bytes"]
    digest, scope = value["sha256"], value["verification_scope"]
    if (value["reference"] != reference or not isinstance(reference, str) or not _REFERENCE.fullmatch(reference)
            or not isinstance(name, str) or not 1 <= len(name) <= 240 or name in {".", ".."}
            or any(ord(c) < 32 or c in "/\\" for c in name)
            or not isinstance(mime, str) or len(mime) > 120
            or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)
            or type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(scope, str) or len(scope) > 160 or any(ord(c) < 32 for c in scope)):
        raise ValueError("ARTIFACT_COPY_CORRUPTED")
    return {key: value[key] for key in keys}


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    @contextmanager
    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.root / "index.sqlite3"), timeout=1)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS artifacts(
                conversation_id TEXT NOT NULL, reference TEXT NOT NULL, metadata TEXT NOT NULL,
                PRIMARY KEY(conversation_id,reference))""")
            with connection:
                yield connection
        finally:
            connection.close()

    def _target(self, conversation_id, reference):
        conversation_uuid(conversation_id)
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise ValueError("INVALID_ARTIFACT_REFERENCE")
        target = self.root / conversation_id / reference
        resolved = target.resolve()
        if (not resolved.is_relative_to(self.root) or resolved != target.absolute()
                or resolved.parent != (self.root / conversation_id).resolve()):
            raise ValueError("INVALID_ARTIFACT_REFERENCE")
        return resolved

    def register(self, conversation_id, report, files):
        if (not isinstance(report, dict) or report.get("passed") is not True
                or not isinstance(report.get("path"), str)):
            raise ValueError("ARTIFACT_NOT_VERIFIED")
        path, raw, digest = files.read_bytes(report["path"], maximum=MAX_ARTIFACT_BYTES)
        if report.get("sha256") != digest:
            raise ValueError("ARTIFACT_CHANGED")
        return self._store_bytes(conversation_id, path, raw, digest, report.get("scope") or "format_and_readback")

    def register_path(self, conversation_id, path, *, workspace, grants=(), expected_hash=None,
                      expected_size=None, presented=False, private_roots=(), private_profile=None):
        """Snapshot a DSH-presented file without the retired goal-judge receipt.

        An official DSH presentation authorizes exactly its delivered file;
        it is not a directory grant. The hash/length bind that authorization
        to bytes observed by the runner. These checks prove delivery integrity,
        not task correctness.
        """
        if not isinstance(path, str) or not path or any(ord(char) < 32 for char in path):
            raise ValueError("INVALID_ARTIFACT_PATH")
        root = Path(workspace).resolve(strict=True)
        target = Path(path)
        if presented and (not target.is_absolute() or not isinstance(expected_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
                or type(expected_size) is not int or not 0 <= expected_size <= MAX_ARTIFACT_BYTES):
            raise ValueError("INVALID_PRESENTED_ARTIFACT")
        if not target.is_absolute():
            target = root / target
        lexical = target.absolute()
        target = target.resolve(strict=True)
        if presented and target != lexical:
            raise ValueError("ARTIFACT_PATH_ALIAS")
        private = tuple(Path(value).resolve() for value in private_roots)
        profile = Path(private_profile).resolve() if private_profile is not None else None
        allowed = [(target, False)] if presented else [(root, True)]
        if not presented:
            for grant in grants:
                if isinstance(grant, dict) and grant.get("access") in {"read", "write"}:
                    allowed.append((Path(grant["path"]).resolve(strict=True), grant.get("directory") is True))

        def permitted(value):
            if any(value == base or value.is_relative_to(base) for base in private):
                return False
            # The packaged app's dedicated workspace is inside its profile.
            # Other profile contents (conversations/configuration) are private.
            if profile is not None and value.is_relative_to(profile) and not value.is_relative_to(root):
                return False
            return any(value == base or directory and value.is_relative_to(base) for base, directory in allowed)

        if not permitted(target) or not target.is_file():
            raise ValueError("ARTIFACT_OUTSIDE_SCOPE")
        with target.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("ARTIFACT_TOO_LARGE")
            if expected_size is not None and info.st_size != expected_size:
                raise ValueError("ARTIFACT_CHANGED")
            # Recheck the opened handle, not just the path before opening.
            # Junction/symlink replacement must not expose another directory.
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes
                query = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
                query.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                query.restype = wintypes.DWORD
                value = ctypes.create_unicode_buffer(32768)
                count = query(msvcrt.get_osfhandle(stream.fileno()), value, len(value), 0)
                if not count or count >= len(value):
                    raise ValueError("ARTIFACT_PATH_UNAVAILABLE")
                actual = value.value
                if actual.startswith("\\\\?\\UNC\\"):
                    actual = "\\\\" + actual[8:]
                elif actual.startswith("\\\\?\\"):
                    actual = actual[4:]
                if not permitted(Path(actual).resolve()):
                    raise ValueError("ARTIFACT_OUTSIDE_SCOPE")
            elif Path("/proc/self/fd").is_dir():
                if not permitted(Path(f"/proc/self/fd/{stream.fileno()}").resolve()):
                    raise ValueError("ARTIFACT_OUTSIDE_SCOPE")
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError("ARTIFACT_TOO_LARGE")
        if expected_size is not None and len(raw) != expected_size:
            raise ValueError("ARTIFACT_CHANGED")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_hash is not None and expected_hash != digest:
            raise ValueError("ARTIFACT_CHANGED")
        return self._store_bytes(conversation_id, target, raw, digest, "immutable_byte_snapshot")

    def _store_bytes(self, conversation_id, path, raw, digest, scope):
        reference = "artifact_" + hashlib.sha256((digest + ":" + path.name).encode("utf-8")).hexdigest()
        target = self._target(conversation_id, reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("ARTIFACT_COPY_CORRUPTED")
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if Path(temporary).exists():
                    Path(temporary).unlink()
        mime = _FIXED_MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        metadata = {"reference": reference, "name": path.name[:240], "mime": mime,
                    "bytes": len(raw), "sha256": digest,
                    "verification_scope": str(scope)[:160]}
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?)",
                               (conversation_id, reference, json.dumps(metadata, ensure_ascii=False)))
        return metadata

    def list(self, conversation_id):
        conversation_uuid(conversation_id)
        if not (self.root / "index.sqlite3").exists():
            return []
        with self._connect() as connection:
            rows = connection.execute("SELECT metadata FROM artifacts WHERE conversation_id=? ORDER BY rowid DESC LIMIT 100",
                                      (conversation_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, conversation_id, reference, *, offset=0, limit=262144):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 262144:
            raise ValueError("INVALID_ARTIFACT_PAGE")
        metadata, raw = self.verified_snapshot(conversation_id, reference)
        if offset > len(raw):
            raise ValueError("INVALID_ARTIFACT_PAGE")
        end = min(len(raw), offset + limit)
        return {**metadata, "offset": offset, "data_base64": base64.b64encode(raw[offset:end]).decode("ascii"),
                "next_offset": end if end < len(raw) else None, "total_bytes": len(raw)}

    def verified_snapshot(self, conversation_id, reference):
        """One bounded, verified immutable byte snapshot; host export only.

        No cache: a mobile get still calls this once for every requested page.
        """
        target = self._target(conversation_id, reference)
        with self._connect() as connection:
            row = connection.execute("SELECT metadata FROM artifacts WHERE conversation_id=? AND reference=?",
                                     (conversation_id, reference)).fetchone()
        if row is None:
            raise ValueError("ARTIFACT_UNAVAILABLE")
        metadata = checked_artifact_metadata(json.loads(row[0]), reference)
        if target.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("ARTIFACT_COPY_CORRUPTED")
        with target.open("rb") as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise ValueError("ARTIFACT_COPY_CORRUPTED")
        return metadata, raw

    def clear(self, conversation_id=None):
        if not (self.root / "index.sqlite3").exists():
            return
        with self._connect() as connection:
            if conversation_id is None:
                rows = connection.execute("SELECT conversation_id,reference FROM artifacts").fetchall()
                connection.execute("DELETE FROM artifacts")
            else:
                rows = connection.execute("SELECT conversation_id,reference FROM artifacts WHERE conversation_id=?",
                                          (conversation_id,)).fetchall()
                connection.execute("DELETE FROM artifacts WHERE conversation_id=?", (conversation_id,))
        for cid, reference in rows:
            self._target(cid, reference).unlink(missing_ok=True)
