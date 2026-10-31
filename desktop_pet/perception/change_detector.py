"""Compare tiny in-memory RGB samples; screenshots never enter the filesystem."""

from dataclasses import dataclass
import hashlib
import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage


@dataclass(frozen=True)
class ChangeResult:
    changed: bool
    score: float
    reason: str
    fingerprint: str


@dataclass(frozen=True)
class _Sample:
    pixels: bytes
    application_id: str
    source_size: tuple[int, int]
    fingerprint: str


class ChangeDetector:
    """Evaluate without committing; accept only when an observation is handled.

    Comparing every observation with the accepted baseline also detects slow,
    accumulated changes which would be missed by comparing adjacent frames.
    The baseline retains only a small RGB sample, never the full screenshot.
    """

    def __init__(self, threshold: float = 0.08, sample_size: tuple[int, int] = (64, 36)) -> None:
        if (
            not isinstance(sample_size, tuple) or len(sample_size) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 256
                   for value in sample_size)
        ):
            raise ValueError("检测采样尺寸必须由两个 1～256 范围内的整数构成。")
        self._sample_size = sample_size
        self._baseline: _Sample | None = None
        self.threshold = threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.01 <= value <= 1):
            raise ValueError("画面变化阈值必须为 0.01～1 范围内的有限数值。")
        self._threshold = float(value)

    def _sample(self, image: QImage, application_id: str) -> _Sample | None:
        if not isinstance(image, QImage) or image.isNull():
            return None
        normalized = image.scaled(
            *self._sample_size, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_RGB888)
        # RGB scanlines may contain padding, so exclude it from both distance
        # and hash calculations. Its contents need not be deterministic.
        raw = bytes(normalized.constBits())
        stride = normalized.bytesPerLine()
        row_length = normalized.width() * 3
        pixels = b"".join(raw[row * stride:row * stride + row_length]
                          for row in range(normalized.height()))
        app = str(application_id).strip().casefold()
        source_size = (image.width(), image.height())
        digest = hashlib.sha256()
        digest.update(app.encode("utf-8", errors="replace"))
        digest.update(f"\0{source_size[0]}x{source_size[1]}\0".encode("ascii"))
        digest.update(pixels)
        return _Sample(pixels, app, source_size, digest.hexdigest())

    def evaluate(self, image: QImage, application_id: str) -> ChangeResult:
        sample = self._sample(image, application_id)
        if sample is None:
            return ChangeResult(False, 0.0, "empty_image", "")
        baseline = self._baseline
        if baseline is None:
            return ChangeResult(True, 1.0, "initial", sample.fingerprint)
        if sample.application_id != baseline.application_id:
            return ChangeResult(True, 1.0, "application_changed", sample.fingerprint)
        if sample.source_size != baseline.source_size:
            return ChangeResult(True, 1.0, "geometry_changed", sample.fingerprint)
        score = sum(abs(current - previous) for current, previous in zip(
            sample.pixels, baseline.pixels,
        )) / (len(sample.pixels) * 255)
        changed = score >= self._threshold
        return ChangeResult(changed, score, "scene_changed" if changed else "stable", sample.fingerprint)

    def accept(self, image: QImage, application_id: str) -> None:
        sample = self._sample(image, application_id)
        if sample is not None:
            self._baseline = sample

    def reset(self) -> None:
        self._baseline = None

