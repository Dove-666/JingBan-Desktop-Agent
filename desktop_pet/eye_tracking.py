"""Optional expression eye rigs with cached clipping and smooth cursor gaze.

Rig files are read only when this object is constructed. Source identity is
checked when a character is assigned, never in the frame or paint loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from .config import PROJECT_ROOT


DEFAULT_RIG_PATH = PROJECT_ROOT / "assets" / "character" / "eyes" / "neutral" / "rig.json"
DEFAULT_NEUTRAL_PATH = PROJECT_ROOT / "assets" / "expressions" / "neutral.png"
# These source names come from application code, never a rig's metadata.
# talk_closed has the same pixels as neutral and uses that cached rig. Happy
# has closed eyes and deliberately keeps its original static artwork.
TRACKED_EXPRESSION_STEMS = ("neutral", "surprised", "sleepy", "angry", "thinking", "talk_open")


def image_fingerprint(image: QImage) -> str:
    """Hash canonical pixel bytes, independent of PNG compression or Qt format."""
    # QPixmap uses premultiplied alpha on Windows. Canonicalize both sides the
    # same way, including otherwise invisible RGB stored beneath alpha=0.
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    return hashlib.sha256(bytes(rgba.constBits())).hexdigest()


def clamped_gaze(dx: float, dy: float, radius_x: float, radius_y: float) -> tuple[float, float]:
    """Map a face-relative vector to a unit ellipse without corner overshoot."""
    if not all(math.isfinite(value) for value in (dx, dy, radius_x, radius_y)):
        raise ValueError("Gaze coordinates must be finite")
    if radius_x <= 0 or radius_y <= 0:
        raise ValueError("Gaze radii must be positive")
    x, y = dx / radius_x, dy / radius_y
    divisor = max(1.0, math.hypot(x, y))
    return x / divisor, y / divisor


def smooth_gaze(current: tuple[float, float], target: tuple[float, float],
                seconds: float, response: float = 12.0) -> tuple[float, float]:
    """Exponential response gives the same result at different frame rates."""
    if not math.isfinite(seconds) or seconds < 0 or not math.isfinite(response) or response <= 0:
        raise ValueError("Gaze timing must be finite and nonnegative")
    weight = -math.expm1(-response * seconds)
    return tuple((1.0 - weight) * start + weight * end for start, end in zip(current, target))


def cursor_in_source(cursor_global: QPointF, window_origin: QPointF, sprite: QRectF,
                     pose: QTransform, canvas: QSize) -> QPointF | None:
    """Undo the same logical Qt transform used to paint the character.

    QCursor.pos(), mapToGlobal(), and widget geometry are already logical
    coordinates. Multiplying them by a monitor's devicePixelRatio would drift
    the eyes on a high-DPI display.
    """
    inverse, invertible = pose.inverted()
    if not invertible or sprite.isEmpty():
        return None
    local = inverse.map(QPointF(cursor_global) - QPointF(window_origin))
    return QPointF((local.x() - sprite.x()) * canvas.width() / sprite.width(),
                   (local.y() - sprite.y()) * canvas.height() / sprite.height())


def _pair(value, name: str, *, positive: bool = False) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must contain two numbers")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
           for item in value):
        raise ValueError(f"{name} must contain finite numbers")
    result = float(value[0]), float(value[1])
    if positive and min(result) <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _read_image(directory: Path, filename) -> QImage:
    if not isinstance(filename, str) or not filename:
        raise ValueError("Rig image filename is missing")
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Rig image must be inside its asset directory")
    image = QImage(str(path))
    if image.isNull() or not image.hasAlphaChannel():
        raise ValueError("Rig images must be valid transparent PNGs")
    image.setDevicePixelRatio(1.0)
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _alpha_rect(image: QImage) -> QRect:
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    raw, stride = alpha.constBits(), alpha.bytesPerLine()
    left, top, right, bottom = image.width(), image.height(), 0, 0
    for y in range(image.height()):
        row = bytes(raw[y * stride:y * stride + image.width()])
        if row.strip(b"\0"):
            left = min(left, image.width() - len(row.lstrip(b"\0")))
            right = max(right, len(row.rstrip(b"\0")))
            top, bottom = min(top, y), y + 1
    return QRect(left, top, right - left, bottom - top) if right > left else QRect()


@dataclass
class EyePart:
    name: str
    center: QPointF
    iris: QImage
    iris_origin: QPointF
    aperture_origin: QPoint
    aperture: QImage
    travel: QPointF
    _layer: QImage = field(init=False, repr=False)
    _last_offset: tuple[float, float] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Only the small aperture rectangle is composited at runtime. The
        # full-canvas aperture is cropped once, retaining its soft alpha.
        self._layer = QImage(self.aperture.size(), QImage.Format.Format_ARGB32_Premultiplied)

    def paint(self, painter: QPainter, gaze: tuple[float, float]) -> None:
        offset = gaze[0] * self.travel.x(), gaze[1] * self.travel.y()
        if self._last_offset != offset:
            self._layer.fill(Qt.GlobalColor.transparent)
            eye_painter = QPainter(self._layer)
            try:
                eye_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                position = self.iris_origin - QPointF(self.aperture_origin) + QPointF(*offset)
                eye_painter.drawImage(position, self.iris)
                eye_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                eye_painter.drawImage(QPoint(), self.aperture)
            finally:
                eye_painter.end()
            self._last_offset = offset
        painter.drawImage(self.aperture_origin, self._layer)


@dataclass
class EyeRig:
    canvas: QSize
    source_pixels_sha256: str
    base: QImage
    eyes: tuple[EyePart, ...]
    face_center: QPointF
    response_radius: QPointF

    def paint(self, painter: QPainter, gaze: tuple[float, float]) -> None:
        painter.drawImage(QPoint(), self.base)
        for eye in self.eyes:
            eye.paint(painter, gaze)


def load_eye_rig(rig_path: Path, source_path: Path) -> EyeRig | None:
    """Missing, stale, or corrupt optional art leaves the original sprite usable."""
    try:
        data = json.loads(Path(rig_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("canvas") != [1024, 1024]:
            return None
        # The caller supplies the known expression path. Optional source_file
        # metadata cannot redirect provenance checks to another image.
        if data.get("source_sha256") != hashlib.sha256(Path(source_path).read_bytes()).hexdigest():
            return None
        source = QImage(str(source_path))
        canvas = QSize(1024, 1024)
        if source.isNull() or source.size() != canvas or not source.hasAlphaChannel():
            return None
        directory = Path(rig_path).parent
        base = _read_image(directory, data["base"])
        if base.size() != canvas or _alpha_rect(base).isEmpty():
            return None
        eye_data = data["eyes"]
        if not isinstance(eye_data, list) or len(eye_data) != 2:
            return None
        eyes = []
        for item in eye_data:
            center = QPointF(*_pair(item["center"], "center"))
            origin = QPointF(*_pair(item["iris_origin"], "iris_origin"))
            travel = QPointF(*_pair(item["travel"], "travel"))
            if min(travel.x(), travel.y()) < 0 or not QRectF(0, 0, 1024, 1024).contains(center):
                return None
            iris = _read_image(directory, item["iris"])
            aperture = _read_image(directory, item["aperture"])
            if (aperture.size() != canvas or iris.width() > 1024 or iris.height() > 1024
                    or _alpha_rect(iris).isEmpty()):
                return None
            bounds = _alpha_rect(aperture)
            if bounds.isEmpty():
                return None
            eyes.append(EyePart(str(item["name"]), center, iris, origin,
                                bounds.topLeft(), aperture.copy(bounds), travel))
        center = sum((eye.center for eye in eyes), QPointF()) / len(eyes)
        # Optional radii are in source pixels, so sensitivity follows the same
        # character scale and pose as the eye artwork.
        radius = QPointF(*_pair(data.get("response_radius", [614.4, 460.8]),
                               "response_radius", positive=True))
        return EyeRig(canvas, image_fingerprint(source), base, tuple(eyes), center, radius)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None


class EyeTracker:
    def __init__(self, rig_path: Path | None = None, neutral_path: Path | None = None) -> None:
        if rig_path is not None or neutral_path is not None:
            # Retain the original explicit single-rig interface for isolated
            # previews/tests. Normal application startup loads the known set.
            candidates = [("neutral", rig_path or DEFAULT_RIG_PATH, neutral_path or DEFAULT_NEUTRAL_PATH)]
        else:
            rig_root, expression_root = DEFAULT_RIG_PATH.parent.parent, DEFAULT_NEUTRAL_PATH.parent
            candidates = [(stem,
                           DEFAULT_RIG_PATH if stem == "neutral" else rig_root / stem / "rig.json",
                           DEFAULT_NEUTRAL_PATH if stem == "neutral" else expression_root / f"{stem}.png")
                          for stem in TRACKED_EXPRESSION_STEMS]
        self.rigs: dict[str, EyeRig] = {}
        self._rigs_by_pixels: dict[str, tuple[str, EyeRig]] = {}
        for name, candidate_path, source_path in candidates:
            rig = load_eye_rig(candidate_path, source_path)
            if rig is not None:
                cached = self._rigs_by_pixels.setdefault(rig.source_pixels_sha256, (name, rig))[1]
                self.rigs[name] = cached
        self.rig_name = ""
        self.rig = next(iter(self.rigs.values()), None)
        self.enabled = True
        self.source_matches = False
        self.gaze = (0.0, 0.0)

    @property
    def has_rigs(self) -> bool:
        return bool(self.rigs)

    @property
    def active(self) -> bool:
        return self.enabled and self.source_matches and self.rig is not None

    def bind_source(self, image: QImage) -> None:
        match = (self._rigs_by_pixels.get(image_fingerprint(image))
                 if self.has_rigs and image.size() == QSize(1024, 1024) else None)
        self.rig_name, self.rig = match if match is not None else ("", None)
        self.source_matches = match is not None
        # Keep normalized direction across expressions, including a temporary
        # closed-eye picture. Each cached rig applies its own safe iris travel.

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.gaze = (0.0, 0.0)

    def advance(self, seconds: float, cursor_source: QPointF, *, suspended: bool = False) -> None:
        if not self.active or suspended:
            return
        delta = cursor_source - self.rig.face_center
        target = clamped_gaze(delta.x(), delta.y(), self.rig.response_radius.x(), self.rig.response_radius.y())
        self.gaze = smooth_gaze(self.gaze, target, seconds)
