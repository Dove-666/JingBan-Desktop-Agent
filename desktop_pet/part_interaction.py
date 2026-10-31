"""Cached, optional expression ear/tail parts with short damped click motion.

All geometry is in the original 1024-pixel source canvas. The window owns
cursor mapping, sequence gating and drag priority; this module performs no
screen access, network access, or file I/O after construction.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from .config import PROJECT_ROOT
from .eye_tracking import image_fingerprint


DEFAULT_RIG_PATH = PROJECT_ROOT / "assets" / "character" / "interactions" / "neutral" / "rig.json"
DEFAULT_SOURCE_PATH = PROJECT_ROOT / "assets" / "expressions" / "neutral.png"
# Source names are trusted application constants, never rig-provided paths.
# talk_closed is a pixel-identical alias of neutral and needs no duplicate rig.
INTERACTION_STEMS = ("neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talk_open")
DURATIONS = {"left_ear": .65, "right_ear": .65, "tail": .9}
DEFAULT_AMPLITUDES = {"left_ear": -8.0, "right_ear": 8.0, "tail": 7.0}
_CANVAS = QRectF(0, 0, 1024, 1024)


def _swing_peak() -> float:
    # On the first positive lobe the log derivative decreases strictly from
    # +infinity to a negative value. Its unique zero is the global amplitude
    # peak: later lobes have a strictly smaller exp(-4u) * (1-u) envelope.
    low, high = 0.0, .125
    for _ in range(48):
        progress = (low + high) / 2
        derivative = -4 - 1 / (1 - progress) + 4 * math.pi / math.tan(4 * math.pi * progress)
        if derivative > 0:
            low = progress
        else:
            high = progress
    progress = (low + high) / 2
    return math.exp(-4 * progress) * (1 - progress) * math.sin(4 * math.pi * progress)


_SWING_PEAK = _swing_peak()


def _alpha_bounds(image: QImage) -> QRectF:
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    raw, stride = alpha.constBits(), alpha.bytesPerLine()
    left, top, right, bottom = image.width(), image.height(), 0, 0
    for y in range(image.height()):
        row = bytes(raw[y * stride:y * stride + image.width()])
        if row.strip(b"\0"):
            left = min(left, image.width() - len(row.lstrip(b"\0")))
            right = max(right, len(row.rstrip(b"\0")))
            top, bottom = min(top, y), y + 1
    return QRectF(left, top, right - left, bottom - top) if right > left else QRectF()


def _pair(value, name: str) -> QPointF:
    if (not isinstance(value, list) or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, (float, int))
                   or not math.isfinite(item) for item in value)):
        raise ValueError(f"{name} must contain two finite source coordinates")
    return QPointF(float(value[0]), float(value[1]))


def _read_image(directory: Path, filename) -> QImage:
    if not isinstance(filename, str) or not filename:
        raise ValueError("A part image filename is missing")
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Part images must stay inside their rig directory")
    image = QImage(str(path))
    if image.isNull() or not image.hasAlphaChannel():
        raise ValueError("Part images must have a valid alpha channel")
    image.setDevicePixelRatio(1.0)
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _alpha_at(image: QImage, point: QPointF, origin: QPointF = QPointF()) -> int:
    if not math.isfinite(point.x()) or not math.isfinite(point.y()):
        return 0
    x, y = math.floor(point.x() - origin.x()), math.floor(point.y() - origin.y())
    return image.pixelColor(x, y).alpha() if 0 <= x < image.width() and 0 <= y < image.height() else 0


def _rotation(pivot: QPointF, degrees: float) -> QTransform:
    transform = QTransform()
    transform.translate(pivot.x(), pivot.y())
    transform.rotate(degrees)
    transform.translate(-pivot.x(), -pivot.y())
    return transform


def _sweep(rect: QRectF, pivot: QPointF, amplitude: float) -> QRectF:
    """Exact rectangular-corner envelope over a bounded rotation interval."""
    low, high = -math.radians(abs(amplitude)), math.radians(abs(amplitude))
    points = []
    for corner in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
        dx, dy = corner.x() - pivot.x(), corner.y() - pivot.y()
        angles = [low, 0.0, high]
        for stationary in (math.atan2(-dy, dx), math.atan2(dx, dy)):
            for half_turn in range(-2, 3):
                angle = stationary + half_turn * math.pi
                if low <= angle <= high:
                    angles.append(angle)
        points.extend(QPointF(pivot.x() + dx * math.cos(angle) - dy * math.sin(angle),
                              pivot.y() + dx * math.sin(angle) + dy * math.cos(angle))
                      for angle in angles)
    left, right = min(point.x() for point in points), max(point.x() for point in points)
    top, bottom = min(point.y() for point in points), max(point.y() for point in points)
    return QRectF(left, top, right - left, bottom - top)


@dataclass
class InteractionPart:
    name: str
    image: QImage
    origin: QPointF
    pivot: QPointF
    hit_mask: QImage | None
    hit_origin: QPointF
    amplitude_degrees: float
    duration: float
    z: str
    alpha_bounds: QRectF

    def transform(self, degrees: float) -> QTransform:
        return _rotation(self.pivot, degrees)

    def contains(self, point: QPointF, degrees: float, *, use_hit_mask: bool = False) -> bool:
        inverse, invertible = self.transform(degrees).inverted()
        if not invertible:
            return False
        unposed = inverse.map(point)
        if not _alpha_at(self.image, unposed, self.origin):
            return False
        return not use_hit_mask or self.hit_mask is None or bool(_alpha_at(self.hit_mask, unposed, self.hit_origin))


@dataclass
class InteractionRig:
    canvas: QSize
    source: QImage
    source_pixels_sha256: str
    source_bounds: QRectF
    base: QImage
    body_bounds: QRectF
    cutout_mask: QImage
    parts: tuple[InteractionPart, ...]
    motion_bounds: QRectF
    repair_masks: dict[str, tuple[QPoint, QImage]]


def load_interaction_rig(rig_path: Path, source_path: Path) -> InteractionRig | None:
    """Validate against the caller's trusted source path, never JSON paths."""
    try:
        data = json.loads(Path(rig_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("canvas") != [1024, 1024]:
            return None
        if data.get("source_sha256") != hashlib.sha256(Path(source_path).read_bytes()).hexdigest():
            return None
        source = QImage(str(source_path))
        canvas = QSize(1024, 1024)
        if source.isNull() or source.size() != canvas or not source.hasAlphaChannel():
            return None
        source = source.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        source_bounds = _alpha_bounds(source)
        if source_bounds.isEmpty():
            return None
        directory = Path(rig_path).parent
        base, mask = _read_image(directory, data["base"]), _read_image(directory, data["cutout_mask"])
        if base.size() != canvas or mask.size() != canvas or _alpha_bounds(mask).isEmpty():
            return None
        body_bounds = _alpha_bounds(base)
        if body_bounds.isEmpty():
            return None
        items = data["parts"]
        if not isinstance(items, list) or not items:
            return None
        parts, names = [], set()
        motion_bounds = source_bounds.united(body_bounds)
        for item in items:
            name = item["name"]
            if name not in DURATIONS or name in names:
                return None
            names.add(name)
            image = _read_image(directory, item["image"])
            if image.width() > canvas.width() or image.height() > canvas.height():
                return None
            origin, pivot = _pair(item["origin"], "origin"), _pair(item["pivot"], "pivot")
            if not _CANVAS.contains(pivot):
                return None
            alpha_bounds = _alpha_bounds(image).translated(origin)
            if alpha_bounds.isEmpty() or not _CANVAS.contains(alpha_bounds):
                return None
            amplitude = item.get("amplitude_degrees", DEFAULT_AMPLITUDES[name])
            if isinstance(amplitude, bool) or not isinstance(amplitude, (int, float)) or not math.isfinite(amplitude):
                return None
            amplitude = max(-18.0, min(float(amplitude), 18.0))
            # Limit close-to-edge parts to a safe angle instead of drawing
            # beyond the transparent source canvas. The actual envelope is
            # small and measured only once, not recomputed from frame pixels.
            if not _CANVAS.contains(_sweep(alpha_bounds, pivot, amplitude)):
                low, high = 0.0, abs(amplitude)
                for _ in range(24):
                    middle = (low + high) / 2
                    if _CANVAS.contains(_sweep(alpha_bounds, pivot, middle)):
                        low = middle
                    else:
                        high = middle
                amplitude = math.copysign(low, amplitude)
            hit_mask, hit_origin = None, QPointF(origin)
            if item.get("hit_mask") is not None:
                hit_mask = _read_image(directory, item["hit_mask"])
                if hit_mask.size() == canvas:
                    hit_origin = QPointF()
                elif hit_mask.size() != image.size():
                    return None
                # Full-canvas Photoshop masks are mostly transparent. Measure
                # once and keep only the original nonzero-alpha pixels, with
                # their source origin preserved for inverse-rotation sampling.
                hit_bounds = _alpha_bounds(hit_mask)
                if hit_bounds.isEmpty():
                    hit_mask = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
                    hit_mask.fill(Qt.GlobalColor.transparent)
                else:
                    crop = hit_bounds.toAlignedRect()
                    hit_origin += QPointF(crop.topLeft())
                    hit_mask = hit_mask.copy(crop)
            z = item.get("z", "behind")
            if z not in {"behind", "front"}:
                return None
            part = InteractionPart(name, image, origin, pivot, hit_mask, hit_origin,
                                   amplitude, DURATIONS[name], z, alpha_bounds)
            parts.append(part)
            motion_bounds = motion_bounds.united(_sweep(alpha_bounds, pivot, amplitude))
        # Keep each repair local to its alpha geometry. A PNG can be a full
        # canvas with only one small opaque part; its file extent is not the
        # repair area. Disjoint official part regions preserve every inactive
        # ear/tail pixel from the original instead of reconstructing it.
        repair_masks = {}
        for part in parts:
            region = part.alpha_bounds.adjusted(-2, -2, 2, 2).intersected(_CANVAS).toAlignedRect()
            repair_masks[part.name] = region.topLeft(), mask.copy(region)
        return InteractionRig(canvas, source, image_fingerprint(source), source_bounds, base,
                              body_bounds, mask, tuple(parts), motion_bounds, repair_masks)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None


class PartInteraction:
    def __init__(self, rig_path: Path | None = None, source_path: Path | None = None) -> None:
        if rig_path is not None or source_path is not None:
            # Preserve the explicit single-rig interface for local previews and
            # synthetic tests. Only normal startup preloads the known bank.
            candidates = [("neutral", rig_path or DEFAULT_RIG_PATH, source_path or DEFAULT_SOURCE_PATH)]
        else:
            rig_root, source_root = DEFAULT_RIG_PATH.parent.parent, DEFAULT_SOURCE_PATH.parent
            candidates = [(stem,
                           DEFAULT_RIG_PATH if stem == "neutral" else rig_root / stem / "rig.json",
                           DEFAULT_SOURCE_PATH if stem == "neutral" else source_root / f"{stem}.png")
                          for stem in INTERACTION_STEMS]
        self.rigs: dict[str, InteractionRig] = {}
        self._rigs_by_pixels: dict[str, tuple[str, InteractionRig]] = {}
        for name, candidate_path, candidate_source in candidates:
            rig = load_interaction_rig(candidate_path, candidate_source)
            if rig is not None:
                cached = self._rigs_by_pixels.setdefault(rig.source_pixels_sha256, (name, rig))[1]
                self.rigs[name] = cached
        self.rig_name = ""
        self.rig = next(iter(self.rigs.values()), None)
        self.source_matches = False
        self._source = QImage()
        self._elapsed: dict[str, float] = {}
        self._parts = {part.name: part for part in self.rig.parts} if self.rig is not None else {}
        # Image keys isolate source/eye bases. Derived subset images are kept
        # only for the selected rig, rather than accumulating across the bank.
        self._body_cache: dict[tuple[int, tuple[str, ...]], QImage] = {}
        self._body_bounds_cache: dict[tuple[int, tuple[str, ...]], QRectF] = {}

    @property
    def has_rigs(self) -> bool:
        return bool(self.rigs)

    @property
    def active(self) -> bool:
        return self.rig is not None and self.source_matches

    @property
    def animating(self) -> bool:
        return self.active and bool(self._elapsed)

    def bind_source(self, image: QImage) -> None:
        previous_rig = self.rig
        self.reset()
        match = (self._rigs_by_pixels.get(image_fingerprint(image))
                 if self.has_rigs and image.size() == QSize(1024, 1024) else None)
        self.rig_name, self.rig = match if match is not None else ("", None)
        if self.rig is not previous_rig:
            # Retain all preloaded layers and fingerprint indexes. A different
            # expression only discards derived Qt composites; a pixel-identical
            # alias of this rig can keep them without disk reads or decoding.
            self._body_cache.clear()
            self._body_bounds_cache.clear()
        self.source_matches = match is not None
        self._parts = {part.name: part for part in self.rig.parts} if self.rig is not None else {}
        self._source = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied) if self.source_matches else QImage()

    def reset(self) -> None:
        self._elapsed.clear()

    def trigger(self, name: str) -> bool:
        if not self.active or name not in self._parts or not self._parts[name].amplitude_degrees:
            return False
        self._elapsed[name] = 0.0
        return True

    def advance(self, seconds: float, suspended: bool = False) -> None:
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Part animation time must be finite and nonnegative")
        if not self.active or suspended:
            return
        for name in tuple(self._elapsed):
            elapsed = self._elapsed[name] + seconds
            if elapsed >= self._parts[name].duration:
                del self._elapsed[name]
            else:
                self._elapsed[name] = elapsed

    def angle(self, name: str) -> float:
        if not self.active or name not in self._elapsed:
            return 0.0
        part = self._parts[name]
        progress = self._elapsed[name] / part.duration
        swing = math.exp(-4 * progress) * (1 - progress) * math.sin(4 * math.pi * progress) / _SWING_PEAK
        return part.amplitude_degrees * max(-1.0, min(1.0, swing))

    def _moving_names(self) -> tuple[str, ...]:
        # Trigger and oscillator zero crossings use the exact canonical pose.
        # The elapsed clock still runs until its normal duration completes.
        return tuple(sorted(name for name in self._elapsed if abs(self.angle(name)) > 1e-9))

    def part_bounds(self, name: str) -> QRectF:
        if not self.active or name not in self._parts:
            return QRectF()
        part = self._parts[name]
        return part.transform(self.angle(name)).mapRect(part.alpha_bounds)

    def visible_bounds(self) -> QRectF:
        if not self.active:
            return QRectF()
        names = self._moving_names()
        if not names:
            return QRectF(self.rig.source_bounds)
        self._body_for_parts(None, names)
        key = (self.rig.source.cacheKey(), names)
        result = QRectF(self._body_bounds_cache[key])
        for name in names:
            result = result.united(self.part_bounds(name))
        return result

    def swept_bounds(self) -> QRectF:
        return QRectF(self.rig.motion_bounds) if self.active else QRectF()

    def hit_test(self, point: QPointF) -> str | None:
        if not self.active:
            return None
        point = QPointF(point)
        moving = self._moving_names()
        for part in reversed(self.rig.parts):
            if part.z == "front" and part.contains(point, self.angle(part.name), use_hit_mask=True):
                return part.name
        for part in reversed(self.rig.parts):
            if part.z == "behind" and part.contains(point, self.angle(part.name), use_hit_mask=True):
                # An inactive ear remains in the canonical image, so remove
                # this candidate from the occlusion image before testing it.
                # Other inactive parts and foreground hair remain intact.
                occlusion_names = tuple(sorted(set(moving) | {part.name}))
                if _alpha_at(self._body_for_parts(None, occlusion_names), point) == 255:
                    continue
                if part.name not in moving and not _alpha_at(self._source, point):
                    continue
                return part.name
        return None

    def contains(self, point: QPointF) -> bool:
        if not self.active:
            return False
        point = QPointF(point)
        names = self._moving_names()
        if not names:
            return bool(_alpha_at(self._source, point))
        return bool(_alpha_at(self._body_for_parts(None, names), point)) or any(
            self._parts[name].contains(point, self.angle(name)) for name in names)

    def _body_for_parts(self, eye_rig, names: tuple[str, ...]) -> QImage:
        canonical = (eye_rig.base if eye_rig is not None
                     and eye_rig.source_pixels_sha256 == self.rig.source_pixels_sha256 else self.rig.source)
        key = canonical.cacheKey(), names
        if key not in self._body_cache:
            mask = QImage(self.rig.canvas, QImage.Format.Format_ARGB32_Premultiplied)
            mask.fill(Qt.GlobalColor.transparent)
            painter = QPainter(mask)
            for name in names:
                position, local_mask = self.rig.repair_masks[name]
                painter.drawImage(position, local_mask)
            painter.end()
            # Patch only currently moving parts. Inactive ears/tail, blank
            # sclera and the rest of the face keep canonical pixels exactly.
            patch = self.rig.base.copy()
            painter = QPainter(patch)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(QPoint(), mask)
            painter.end()
            body = canonical.copy()
            painter = QPainter(body)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            painter.drawImage(QPoint(), mask)
            # Add the two premultiplied terms instead of SourceOver: a soft
            # repair mask must not punch semi-transparent holes in an opaque
            # body where both source layers are opaque.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            painter.drawImage(QPoint(), patch)
            painter.end()
            self._body_cache[key] = body
            self._body_bounds_cache[key] = _alpha_bounds(body)
        return self._body_cache[key]

    def paint(self, painter: QPainter, eye_rig=None, gaze=(0.0, 0.0)) -> bool:
        if not self.active:
            return False
        matching_eyes = (eye_rig is not None and eye_rig.source_pixels_sha256 == self.rig.source_pixels_sha256)
        names = self._moving_names()
        if not names:
            # Rest is the exact bound source, not an approximate reconstruction.
            if matching_eyes:
                eye_rig.paint(painter, gaze)
            else:
                painter.drawImage(QPoint(), self._source)
            return True
        painter.save()
        try:
            painter.setClipRect(_CANVAS, Qt.ClipOperation.IntersectClip)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            for layer in ("behind", "front"):
                if layer == "front":
                    painter.drawImage(QPoint(), self._body_for_parts(eye_rig if matching_eyes else None, names))
                for part in self.rig.parts:
                    if part.z == layer and part.name in names:
                        painter.save()
                        painter.setTransform(part.transform(self.angle(part.name)), combine=True)
                        painter.drawImage(part.origin, part.image)
                        painter.restore()
            if matching_eyes:
                for eye in eye_rig.eyes:
                    eye.paint(painter, gaze)
        finally:
            painter.restore()
        return True
