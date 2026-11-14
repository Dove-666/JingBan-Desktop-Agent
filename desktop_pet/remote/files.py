"""Conversation-scoped document inbox; uploads never name a local path."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import threading
from uuid import UUID

from .errors import RemoteError

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CHUNK_BYTES = 256 * 1024
MAX_INBOX_BYTES = 256 * 1024 * 1024
MAX_INBOX_FILES = 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")


def _scope(peer, cid, digest):
    try:
        valid = (isinstance(peer, str) and _HASH.fullmatch(peer) and isinstance(cid, str)
                 and str(UUID(cid)) == cid and UUID(cid).int != 0
                 and isinstance(digest, str) and _HASH.fullmatch(digest))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise RemoteError("invalid_file", "文件或会话标识无效。")


def _filename(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or any(ord(c) < 32 for c in value):
        raise RemoteError("invalid_file", "文件名称无效。")
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r'[<>:"/\\|?*]', "_", value).strip(" .")
    if not value or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise RemoteError("invalid_file", "文件名称无效。")
    return value


class FileInbox:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self._lock = threading.RLock()

    def _db(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.resolve() != self.directory:
            raise RemoteError("storage_error", "文件接收目录不可用。")
        database = self.directory / "files.sqlite3"
        if database.resolve() != database:
            raise RemoteError("storage_error", "文件接收目录不可用。")
        db = sqlite3.connect(database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS files (
            peer TEXT NOT NULL, cid TEXT NOT NULL, digest TEXT NOT NULL,
            filename TEXT NOT NULL, mime_type TEXT NOT NULL, size INTEGER NOT NULL,
            received INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(peer,cid,digest))""")
        return db

    def _path(self, peer, cid, digest):
        # Every component is derived from validated host-independent identifiers.
        name = hashlib.sha256(f"{peer}:{cid}:{digest}".encode()).hexdigest()
        path = self.directory / (name + ".bin")
        if path.resolve() != path:
            raise RemoteError("storage_error", "文件接收目录不可用。")
        return path

    @staticmethod
    def _public(row):
        return {"id": row["digest"], "sha256": row["digest"], "filename": row["filename"],
                "mime_type": row["mime_type"], "size": row["size"],
                "offset": row["received"], "complete": bool(row["complete"])}

    @staticmethod
    def _verified_bytes(path, size, digest):
        if not path.is_file() or path.stat().st_size != size:
            raise RemoteError("file_checksum", "已接收文件发生变化，请重新上传。")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise RemoteError("file_checksum", "文件校验失败，请重新选择并发送。")
        return raw

    def _remove_missing_records(self, db):
        # A user may remove inbox data to reclaim space. Drop only entries whose
        # host-derived files are gone; never remove an existing uploaded file.
        for row in db.execute("SELECT peer,cid,digest FROM files").fetchall():
            if not self._path(row["peer"], row["cid"], row["digest"]).exists():
                db.execute("DELETE FROM files WHERE peer=? AND cid=? AND digest=?", tuple(row))

    def upload(self, peer, params):
        if set(params) != {"conversation_id", "filename", "mime_type", "size", "sha256", "offset", "data_base64"}:
            raise RemoteError("invalid_file", "文件上传参数无效。")
        cid, digest = params["conversation_id"], params["sha256"]
        _scope(peer, cid, digest)
        filename = _filename(params["filename"])
        mime, size, offset, encoded = (params[key] for key in ("mime_type", "size", "offset", "data_base64"))
        if (not isinstance(mime, str) or len(mime) > 120 or not _MIME.fullmatch(mime)
                or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or type(offset) is not int or not 0 <= offset <= size
                or not isinstance(encoded, str) or len(encoded) > (MAX_CHUNK_BYTES + 2) // 3 * 4):
            raise RemoteError("invalid_file", "文件不能超过 32 MiB，单次分块不能超过 256 KiB。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise RemoteError("invalid_file", "文件分块编码无效。") from None
        if len(data) > MAX_CHUNK_BYTES or offset + len(data) > size or not data and size:
            raise RemoteError("invalid_file", "文件分块长度无效。")
        with self._lock:
            db = self._db()
            try:
                with db:
                    row = db.execute("SELECT * FROM files WHERE peer=? AND cid=? AND digest=?", (peer, cid, digest)).fetchone()
                    path = self._path(peer, cid, digest)
                    if row is not None and not path.exists() and offset == 0:
                        db.execute("DELETE FROM files WHERE peer=? AND cid=? AND digest=?", (peer, cid, digest))
                        row = None
                    if row is None:
                        if offset != 0:
                            raise RemoteError("file_offset", "请从文件开头重新上传。")
                        used, count = db.execute("SELECT coalesce(sum(size),0),count(*) FROM files").fetchone()
                        if used + size > MAX_INBOX_BYTES or count >= MAX_INBOX_FILES:
                            self._remove_missing_records(db)
                            used, count = db.execute("SELECT coalesce(sum(size),0),count(*) FROM files").fetchone()
                        if used + size > MAX_INBOX_BYTES or count >= MAX_INBOX_FILES:
                            raise RemoteError("storage_full", "电脑文件接收空间已满，请清理接收目录后重试。")
                        db.execute("INSERT INTO files(peer,cid,digest,filename,mime_type,size) VALUES(?,?,?,?,?,?)",
                                   (peer, cid, digest, filename, mime, size))
                        # An orphaned partial from a failed transaction is safe to replace.
                        with path.open("wb"):
                            pass
                        row = db.execute("SELECT * FROM files WHERE peer=? AND cid=? AND digest=?", (peer, cid, digest)).fetchone()
                    if (row["filename"], row["mime_type"], row["size"]) != (filename, mime, size):
                        raise RemoteError("file_conflict", "本会话已上传同内容的另一文件名或格式，请沿用原文件或新建会话。")
                    if not path.is_file() or path.stat().st_size < row["received"]:
                        raise RemoteError("file_unavailable", "电脑接收文件不完整，请重新上传。")
                    if offset < row["received"] or row["complete"]:
                        with path.open("rb") as stream:
                            stream.seek(offset)
                            if offset + len(data) > row["received"] or stream.read(len(data)) != data:
                                raise RemoteError("file_conflict", "文件分块与已接收内容不一致。")
                        return self._public(row)
                    if offset != row["received"]:
                        raise RemoteError("file_offset", "文件分块顺序无效，请重试当前文件。")
                    with path.open("r+b") as stream:
                        stream.seek(offset)
                        stream.write(data)
                        stream.truncate(offset + len(data))
                        stream.flush()
                        os.fsync(stream.fileno())
                    received = offset + len(data)
                    complete = received == size
                    if complete:
                        # Failure keeps the previous committed cursor, so the
                        # final chunk can be corrected without duplicate work.
                        self._verified_bytes(path, size, digest)
                    db.execute("UPDATE files SET received=?,complete=? WHERE peer=? AND cid=? AND digest=?",
                               (received, int(complete), peer, cid, digest))
                    return self._public(db.execute("SELECT * FROM files WHERE peer=? AND cid=? AND digest=?", (peer, cid, digest)).fetchone())
            finally:
                db.close()

    def metadata(self, peer, cid, digest):
        _scope(peer, cid, digest)
        with self._lock:
            db = self._db()
            try:
                row = db.execute("SELECT * FROM files WHERE peer=? AND cid=? AND digest=? AND complete=1", (peer, cid, digest)).fetchone()
                if row is None:
                    raise RemoteError("file_unavailable", "文件尚未上传完成，或不属于本次会话。")
                return self._public(row)
            finally:
                db.close()

    def materialize(self, peer, cid, digest, workspace):
        """Copy verified bytes into this task's workspace, without opening them."""
        with self._lock:
            metadata = self.metadata(peer, cid, digest)
            raw = self._verified_bytes(self._path(peer, cid, digest), metadata["size"], digest)
            root = Path(workspace).expanduser().resolve()
            folder = root / ".aura-attachments" / cid / digest
            if folder.resolve() != folder or not folder.resolve().is_relative_to(root):
                raise RemoteError("storage_error", "任务附件目录不可用。")
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / metadata["filename"]
            if target.resolve() != target:
                raise RemoteError("storage_error", "任务附件位置不可用。")
            try:
                self._verified_bytes(target, metadata["size"], digest)
                matches = True
            except RemoteError:
                matches = False
            if not matches:
                temporary = folder / ".upload.tmp"
                if temporary.resolve() != temporary:
                    raise RemoteError("storage_error", "任务附件位置不可用。")
                with temporary.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            return {"path": str(target), "name": metadata["filename"], "size": len(raw), "sha256": digest}
