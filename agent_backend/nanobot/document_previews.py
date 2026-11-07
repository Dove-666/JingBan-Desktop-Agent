"""Bounded in-memory page capabilities for already authorized rendered PDFs.

No process, file write, URL fetch or model call occurs here. A reference only
reveals a page while the original PDF is still readable at its original hash.
Image bytes never belong in the parent transcript or public activity frames.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
import base64
import hashlib
import secrets
import struct
import threading
import zlib


class PreviewError(ValueError):
    def __init__(self, code): self.code = code; super().__init__(code)


def _png_size(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 8*1024**2 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PreviewError("DOCUMENT_PREVIEW_INVALID")
    offset, count, image, dimensions = 8, 0, False, None
    while offset < len(raw):
        count += 1
        if count > 4096 or len(raw)-offset < 12: raise PreviewError("DOCUMENT_PREVIEW_INVALID")
        length = struct.unpack_from(">I", raw, offset)[0]
        end = offset + 12 + length
        if end > len(raw): raise PreviewError("DOCUMENT_PREVIEW_INVALID")
        kind, payload = raw[offset+4:offset+8], raw[offset+8:offset+8+length]
        crc = struct.unpack_from(">I", raw, offset+8+length)[0]
        if zlib.crc32(kind+payload) & 0xffffffff != crc: raise PreviewError("DOCUMENT_PREVIEW_INVALID")
        if count == 1:
            if kind != b"IHDR" or length != 13: raise PreviewError("DOCUMENT_PREVIEW_INVALID")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (not 1 <= width <= 1800 or not 1 <= height <= 1800 or depth not in depths.get(color, set())
                    or compression != 0 or filtering != 0 or interlace not in {0, 1}):
                raise PreviewError("DOCUMENT_PREVIEW_INVALID")
            dimensions = width, height
        elif kind == b"IHDR": raise PreviewError("DOCUMENT_PREVIEW_INVALID")
        if kind == b"IDAT" and length: image = True
        if kind == b"IEND":
            if length or end != len(raw) or not image: raise PreviewError("DOCUMENT_PREVIEW_INVALID")
            return dimensions
        offset = end
    raise PreviewError("DOCUMENT_PREVIEW_INVALID")


@dataclass(frozen=True)
class PreviewImage:
    reference: str
    page: int
    pdf_sha256: str
    png_sha256: str
    width: int
    height: int
    _png: bytes = field(repr=False)

    @property
    def data_url(self): return "data:image/png;base64," + base64.b64encode(self._png).decode("ascii")


@dataclass(frozen=True)
class _Entry:
    image: PreviewImage
    requested_path: str
    target: object


class PagePreviewCache:
    def __init__(self, files, *, max_bytes=32*1024**2, max_pages=64):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32*1024**2 or type(max_pages) is not int or not 1 <= max_pages <= 64:
            raise ValueError("DOCUMENT_PREVIEW_LIMIT")
        self._files, self._max_bytes, self._max_pages = files, max_bytes, max_pages
        self._entries, self._documents = {}, OrderedDict()
        self._bytes, self._lock = 0, threading.RLock()

    @property
    def retained_bytes(self):
        with self._lock: return self._bytes

    def _remove(self, target):
        for reference in self._documents.pop(target, ()):
            self._bytes -= len(self._entries.pop(reference).image._png)

    def clear(self):
        with self._lock:
            self._entries.clear(); self._documents.clear(); self._bytes = 0

    def publish(self, path, pdf_sha256, png_pages, *, pages):
        if (type(path) is not str or type(pdf_sha256) is not str or type(pages) is not int
                or not 1 <= pages <= 20 or type(png_pages) not in {list, tuple} or len(png_pages) != pages):
            raise PreviewError("DOCUMENT_PREVIEW_INVALID")
        prepared = [(raw, _png_size(raw)) for raw in png_pages]
        size = sum(len(raw) for raw, _ in prepared)
        if size > self._max_bytes or pages > self._max_pages: raise PreviewError("DOCUMENT_PREVIEW_LIMIT")
        # Use the same order as publication: file capability before cache lock.
        with self._files._lock:
            try: target, pdf, digest = self._files.read_bytes(path)
            except Exception: raise PreviewError("DOCUMENT_PREVIEW_UNAVAILABLE") from None
            if digest != pdf_sha256 or not pdf.startswith(b"%PDF-"):
                raise PreviewError("DOCUMENT_PREVIEW_STALE")
            with self._lock:
                if target in self._documents: self._remove(target)
                while self._documents and (self._bytes+size > self._max_bytes or len(self._entries)+pages > self._max_pages):
                    self._remove(next(iter(self._documents)))
                references, metadata = [], []
                for index, (raw, (width, height)) in enumerate(prepared, 1):
                    reference = "page_" + secrets.token_hex(16)
                    image = PreviewImage(reference, index, digest, hashlib.sha256(raw).hexdigest(), width, height, raw)
                    self._entries[reference] = _Entry(image, path, target)
                    references.append(reference); self._bytes += len(raw)
                    metadata.append({"reference": reference, "page": index, "width": width, "height": height,
                        "pdf_sha256": digest, "png_sha256": image.png_sha256})
                self._documents[target] = tuple(references)
                return metadata

    def image(self, reference):
        if type(reference) is not str: raise PreviewError("DOCUMENT_PREVIEW_UNAVAILABLE")
        with self._files._lock:
            with self._lock:
                entry = self._entries.get(reference)
                if entry is None: raise PreviewError("DOCUMENT_PREVIEW_UNAVAILABLE")
                try: target, _, digest = self._files.read_bytes(entry.requested_path)
                except Exception: raise PreviewError("DOCUMENT_PREVIEW_UNAVAILABLE") from None
                if target != entry.target or digest != entry.image.pdf_sha256:
                    raise PreviewError("DOCUMENT_PREVIEW_STALE")
                self._documents.move_to_end(entry.target)
                return entry.image
