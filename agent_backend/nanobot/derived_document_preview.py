"""Explicit bounded host image derivation, not an authorization or model call.

The original PDF/PNG is never mutated. Callers retain the page cache's before/
after PDF capability checks. No path, SDK, network, environment or file IO is
used here; Pillow is loaded lazily and is never installed automatically.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import io
import math
import struct

try:
    from .document_previews import _png_size
except ImportError:
    from document_previews import _png_size

MAX_INPUT_BYTES = 8 * 1024**2
MAX_EDGE = 1800
MAX_PIXELS = MAX_EDGE * MAX_EDGE
MAX_DATA_URL_CHARACTERS = 1_000_000
# Fixed host policy. No model-selected quality, crop, encoder or dimensions.
JPEG_TIERS = ((1800, 85), (1800, 75), (1400, 85), (1400, 75),
              (1100, 85), (1100, 75), (900, 80), (900, 65), (700, 75), (512, 60))


class DerivedPreviewError(ValueError):
    def __init__(self, code): self.code = code; super().__init__(code)


@dataclass(frozen=True)
class DerivedDocumentPreview:
    original_png_sha256: str
    sent_image_sha256: str
    source_width: int
    source_height: int
    derived_width: int
    derived_height: int
    content_type: str
    derived: bool
    transform: str
    attempts: int
    _image_bytes: bytes = field(repr=False)

    @property
    def image_bytes(self): return self._image_bytes

    @property
    def data_url(self):
        # Computed only for the immediate host wire request, never stored.
        return "data:" + self.content_type + ";base64," + base64.b64encode(self._image_bytes).decode("ascii")

    def metadata(self):
        return {"original_png_sha256": self.original_png_sha256, "sent_image_sha256": self.sent_image_sha256,
            "source_width": self.source_width, "source_height": self.source_height,
            "derived_width": self.derived_width, "derived_height": self.derived_height,
            "content_type": self.content_type, "derived": self.derived, "transform": self.transform,
            "attempts": self.attempts, "bytes": len(self._image_bytes),
            "reference_only": True, "visual_layout_verified": False}


class _CandidateTooLarge(Exception): pass


class _BoundedOutput(io.BytesIO):
    def __init__(self, maximum): super().__init__(); self.maximum = maximum
    def write(self, value):
        if self.tell() + len(value) > self.maximum: raise _CandidateTooLarge()
        return super().write(value)


def _check_cancel(cancel):
    if cancel is not None:
        try: stopped = cancel.is_set()
        except Exception: raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INVALID_ARGUMENT") from None
        if stopped: raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_CANCELLED")


def _fits(raw, content_type):
    return len("data:" + content_type + ";base64,") + 4 * ((len(raw) + 2) // 3) <= MAX_DATA_URL_CHARACTERS


def _fit(width, height, edge):
    ratio = min(1.0, edge / max(width, height))
    return max(1, math.floor(width * ratio + .5)), max(1, math.floor(height * ratio + .5))


def _load_pillow():
    try:
        from PIL import Image
        return Image
    except Exception: raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_UNAVAILABLE") from None


def _reject_animation_chunks(raw):
    # _png_size has already verified the complete bounded chunk structure/CRCs.
    # Inspect chunk types, not payload words or Pillow's declared frame count:
    # malformed acTL counts can otherwise make a multi-frame stream look static.
    offset = 8
    while offset < len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        if raw[offset+4:offset+8] in {b"acTL", b"fcTL", b"fdAT"}:
            raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INVALID")
        offset += 12 + length


def derive_document_preview(png_bytes, *, cancel=None):
    """Return exact small PNG or labelled white-matte, full-canvas JPEG.

    Cancellation is checked before/after bounded decode and each encode tier.
    This synchronous converter never sends a request or retries a model call.
    """
    _check_cancel(cancel)
    if type(png_bytes) is not bytes or not png_bytes:
        raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INVALID_ARGUMENT")
    if len(png_bytes) > MAX_INPUT_BYTES:
        raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INPUT_LIMIT")
    if png_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(png_bytes) >= 24:
        width, height = struct.unpack_from(">II", png_bytes, 16)
        if width > MAX_EDGE or height > MAX_EDGE or width * height > MAX_PIXELS:
            raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INPUT_LIMIT")
    try: width, height = _png_size(png_bytes)
    except Exception: raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INVALID") from None
    _reject_animation_chunks(png_bytes)
    Image = _load_pillow()
    original_sha = hashlib.sha256(png_bytes).hexdigest()
    rgb = None
    try:
        with Image.open(io.BytesIO(png_bytes)) as source:
            if source.format != "PNG" or source.size != (width, height) or getattr(source, "is_animated", False):
                raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INVALID")
            source.load()  # CRC/header checks alone do not verify compressed pixels.
            _check_cancel(cancel)
            if _fits(png_bytes, "image/png"):
                return DerivedDocumentPreview(original_sha, original_sha, width, height, width, height,
                    "image/png", False, "original_png_exact_v1", 0, png_bytes)
            # Alpha (including palette transparency) is explicitly composited
            # onto white. No orientation guess, crop or metadata is propagated.
            rgba = source.convert("RGBA")
            mask = None
            try:
                rgb = Image.new("RGB", source.size, (255, 255, 255))
                mask = rgba.getchannel("A")
                rgb.paste(rgba, (0, 0), mask)
            finally:
                if mask is not None: mask.close()
                rgba.close()
        _check_cancel(cancel)
        maximum = ((MAX_DATA_URL_CHARACTERS - len("data:image/jpeg;base64,")) // 4) * 3
        attempted, attempt = set(), 0
        for edge, quality in JPEG_TIERS:
            _check_cancel(cancel)
            dimensions = _fit(width, height, edge)
            if (dimensions, quality) in attempted: continue
            attempted.add((dimensions, quality)); attempt += 1
            candidate = rgb if dimensions == rgb.size else rgb.resize(dimensions, Image.Resampling.LANCZOS)
            try:
                _check_cancel(cancel)
                try:
                    with _BoundedOutput(maximum) as output:
                        candidate.save(output, format="JPEG", quality=quality, subsampling=0,
                            optimize=False, progressive=False)
                        raw = output.getvalue()
                except _CandidateTooLarge:
                    _check_cancel(cancel)
                    continue
            finally:
                if candidate is not rgb: candidate.close()
            _check_cancel(cancel)
            if _fits(raw, "image/jpeg"):
                return DerivedDocumentPreview(original_sha, hashlib.sha256(raw).hexdigest(), width, height,
                    dimensions[0], dimensions[1], "image/jpeg", True,
                    "white_matte_full_canvas_fit%s_jpeg_q%s_444_v1" % (edge, quality), attempt, raw)
        raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_OUTPUT_LIMIT")
    except DerivedPreviewError: raise
    except Exception: raise DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_INVALID") from None
    finally:
        if rgb is not None: rgb.close()
