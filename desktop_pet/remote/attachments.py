"""Bounded image inbox. Names from peers never become filesystem paths."""

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import threading
from datetime import datetime, timezone

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

from .errors import RemoteError
from ..chat_images import IMAGE_ID


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 16_000_000
FORMATS = {"png": ("image/png", ".png"), "jpeg": ("image/jpeg", ".jpg"),
           "webp": ("image/webp", ".webp")}


class ImageInbox:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self._decode_lock = threading.Lock()
        self._reconciled = False

    def _db(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.directory / "images.sqlite3"), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS images (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime_type TEXT NOT NULL,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL, width INTEGER NOT NULL,
            height INTEGER NOT NULL, created_at TEXT NOT NULL, extension TEXT NOT NULL)""")
        if not self._reconciled:
            try:
                known = {row["id"]: row["extension"] for row in db.execute("SELECT id,extension FROM images")}
                for path in self.directory.iterdir():
                    match = re.fullmatch(r"([0-9a-f]{64})(\.(?:png|jpg|webp))?(\.(?:tmp|deleting))?", path.name)
                    if not match or not path.is_file():
                        continue
                    image_id, extension, stage = match.groups()
                    if stage == ".deleting" and image_id in known:
                        os.replace(path, self.directory / (image_id + known[image_id]))
                    elif stage or image_id not in known:
                        path.unlink(missing_ok=True)
                self._reconciled = True
            except OSError:
                db.close()
                raise RemoteError("storage_error", "无法恢复图片接收目录，请检查本地目录权限。") from None
        return db

    def _validate_image(self, data):
        # A tiny compressed image can expand to tens of MiB. Decode only one
        # at a time, and release its pixels before retaining the original file.
        with self._decode_lock:
            buffer = QBuffer()
            buffer.setData(QByteArray(data))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer)
            image_format = bytes(reader.format()).decode("ascii", errors="ignore").lower()
            size = reader.size()
            if (image_format not in FORMATS or not size.isValid()
                    or size.width() * size.height() > MAX_PIXELS or reader.imageCount() > 1):
                raise RemoteError("invalid_image", "仅接收有效的静态 PNG、JPEG、WebP 图片，最多 1600 万像素。")
            decoded = reader.read()
            if decoded.isNull():
                raise RemoteError("invalid_image", "无法解码图片，请检查文件是否完整。")
            return image_format, size.width(), size.height()

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ("id", "filename", "mime_type", "size", "sha256",
                                         "width", "height", "created_at")}

    def upload(self, params):
        encoded = params.get("data_base64")
        filename = params.get("filename", "image")
        if (not isinstance(encoded, str) or not encoded or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4
                or not isinstance(filename, str) or not 1 <= len(filename) <= 160
                or any(ord(c) < 32 for c in filename)):
            raise RemoteError("invalid_image", "图片格式无效或超过 4 MiB。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise RemoteError("invalid_image", "图片编码无效。") from None
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise RemoteError("invalid_image", "图片不能超过 4 MiB。")
        digest = hashlib.sha256(data).hexdigest()
        if params.get("sha256") not in (None, digest):
            raise RemoteError("invalid_image", "图片校验失败，请重新发送。")
        image_format, width, height = self._validate_image(data)
        mime_type, extension = FORMATS[image_format]
        if params.get("mime_type") not in (None, "", mime_type):
            raise RemoteError("invalid_image", "图片类型与文件内容不一致。")
        # Keep only a display label, never a remote-controlled directory.
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1] or "image"
        with self._lock:
            db = self._db()
            destination = temporary = None
            try:
                with db:
                    row = db.execute("SELECT * FROM images WHERE id = ?", (digest,)).fetchone()
                    if row:
                        return self._public(row)
                    used = db.execute("SELECT coalesce(sum(size),0) FROM images").fetchone()[0]
                    if used + len(data) > MAX_TOTAL_BYTES:
                        raise RemoteError("storage_full", "接收图片已达 64 MiB，请清理接收文件后重试。")
                    destination = self.directory / (digest + extension)
                    temporary = destination.with_suffix(".tmp")
                    with temporary.open("wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, destination)
                    db.execute("INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?)", (digest, filename, mime_type,
                        len(data), digest, width, height,
                        datetime.now(timezone.utc).isoformat(), extension))
                    return self._public(db.execute("SELECT * FROM images WHERE id = ?", (digest,)).fetchone())
            except Exception:
                for path in (temporary, destination):
                    if path is not None:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            self._reconciled = False
                raise
            finally:
                db.close()

    def usage(self):
        """Worker-only logical totals, using the same store recovery as list.

        Count the entire metadata table, not one page or physical disk blocks.
        A failed query is unavailable, never an apparently empty inbox.
        """
        with self._lock:
            try:
                db = self._db()
                try:
                    row = db.execute("SELECT count(*),coalesce(sum(size),0) FROM images").fetchone()
                    count, used = row[0], row[1]
                    if type(count) is not int or count < 0 or type(used) is not int or used < 0:
                        raise RemoteError("storage_error", "无法读取图片存储用量，请检查本地存储后重试。")
                    return {"count": count, "used_bytes": used, "limit_bytes": MAX_TOTAL_BYTES}
                finally:
                    db.close()
            except (sqlite3.Error, OSError):
                raise RemoteError("storage_error", "无法读取图片存储用量，请检查本地存储后重试。") from None

    def list(self, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise RemoteError("invalid_request", "图片分页参数无效。")
        with self._lock:
            db = self._db()
            try:
                rows = db.execute("SELECT * FROM images ORDER BY created_at,id LIMIT ? OFFSET ?",
                                  (limit + 1, offset)).fetchall()
                return {"items": [self._public(row) for row in rows[:limit]], "has_more": len(rows) > limit,
                        "next_offset": offset + min(limit, len(rows))}
            finally:
                db.close()

    def metadata(self, image_id):
        """Check a reference without loading its image bytes on the UI thread."""
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise RemoteError("invalid_image", "图片标识无效。")
        with self._lock:
            db = self._db()
            try:
                row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
                if row is None or not (self.directory / (row["id"] + row["extension"])).is_file():
                    raise RemoteError("image_missing", "引用的图片不存在或已删除，请重新发送图片后重试。")
                return self._public(row)
            finally:
                db.close()

    def get(self, image_id):
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise RemoteError("invalid_image", "图片标识无效。")
        with self._lock:
            db = self._db()
            try:
                row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
                if row is None:
                    raise RemoteError("not_found", "图片不存在。")
                try:
                    path = self.directory / (row["id"] + row["extension"])
                    if path.stat().st_size > MAX_IMAGE_BYTES:
                        raise RemoteError("storage_error", "本地图片大小无效，请重新传输。")
                    with path.open("rb") as handle:
                        data = handle.read(MAX_IMAGE_BYTES + 1)
                except FileNotFoundError:
                    raise RemoteError("not_found", "图片不存在。") from None
                except OSError:
                    raise RemoteError("storage_error", "无法读取本地图片，请重新传输。") from None
                if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise RemoteError("storage_error", "本地图片校验失败，请重新传输。")
                return {**self._public(row), "data_base64": base64.b64encode(data).decode("ascii")}
            finally:
                db.close()

    def delete(self, image_id):
        with self._lock:
            db = self._db()
            destination = staged = None
            try:
                with db:
                    row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
                    if row:
                        destination = self.directory / (row["id"] + row["extension"])
                        staged = destination.with_name(destination.name + ".deleting")
                        if destination.exists():
                            os.replace(destination, staged)
                        db.execute("DELETE FROM images WHERE id = ?", (image_id,))
                if staged is not None:
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError:
                        self._reconciled = False
                return {"deleted": True, "id": image_id}
            except Exception:
                if staged is not None and staged.exists():
                    try:
                        os.replace(staged, destination)
                    except OSError:
                        self._reconciled = False
                raise
            finally:
                db.close()
