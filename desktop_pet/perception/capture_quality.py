"""Local pixel checks; unusable black frames never need a model request."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage


def is_near_black(image: QImage, excluded_rects=()) -> bool:
    if image.isNull():
        return True
    sample = image.scaled(48, 32, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    dark = sampled = 0
    for y in range(sample.height()):
        for x in range(sample.width()):
            source_x = (x + 0.5) * image.width() / sample.width()
            source_y = (y + 0.5) * image.height() / sample.height()
            if any(left <= source_x < right and top <= source_y < bottom
                   for left, top, right, bottom in excluded_rects):
                continue
            color = sample.pixelColor(x, y)
            sampled += 1
            dark += max(color.red(), color.green(), color.blue()) <= 12
    return not sampled or dark / sampled >= 0.985
