"""Cardinal tooltip geometry adapted from niklashenning/pyqttooltip (MIT).

Upstream function: PlacementUtils.__get_tooltip_rect, at pinned commit
c73d16404b2621e5f4a2faba8755a9c26c10135c:
https://github.com/niklashenning/pyqttooltip/blob/c73d16404b2621e5f4a2faba8755a9c26c10135c/src/pyqttooltip/placement_utils.py

Adaptations: accept global QRectF/QSizeF values instead of mapping QWidget
coordinates; use PySide6 QtCore directly; retain continuous float extents;
fix upstream LEFT/RIGHT vertical centering to subtract size.height()/2,
rather than size.width()/2. Screen selection and fallback scoring belong to
the caller, and a caller whose size already includes tails passes triangle=0.

Copyright (c) 2024 Niklas Henning
SPDX-License-Identifier: MIT
The complete original notice is distributed in third_party/pyqttooltip/LICENSE.
"""

from PySide6.QtCore import QPointF, QRectF, QSizeF


def get_tooltip_rect(reference: QRectF, placement: str, size: QSizeF,
                     triangle_size: float = 0.0, offset: QPointF | None = None) -> QRectF:
    """Port of upstream's four placement branches, without GUI/screen access."""
    reference, size = QRectF(reference), QSizeF(size)
    offset = QPointF() if offset is None else QPointF(offset)
    rect = QRectF()
    if placement == "top":
        rect.setRect(reference.x() + reference.width() / 2 - size.width() / 2 + offset.x(),
                     reference.y() - size.height() - triangle_size + offset.y(),
                     size.width(), size.height() + triangle_size)
    elif placement == "bottom":
        rect.setRect(reference.x() + reference.width() / 2 - size.width() / 2 + offset.x(),
                     reference.y() + reference.height() + offset.y(),
                     size.width(), size.height() + triangle_size)
    elif placement == "left":
        rect.setRect(reference.x() - size.width() - triangle_size + offset.x(),
                     reference.y() + reference.height() / 2 - size.height() / 2 + offset.y(),
                     size.width() + triangle_size, size.height())
    elif placement == "right":
        rect.setRect(reference.x() + reference.width() + offset.x(),
                     reference.y() + reference.height() / 2 - size.height() / 2 + offset.y(),
                     size.width() + triangle_size, size.height())
    else:
        raise ValueError("Tooltip placement must be top, bottom, left, or right")
    return rect
