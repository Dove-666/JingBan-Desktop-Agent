# PyQt Tooltip adaptation

Source: [niklashenning/pyqttooltip](https://github.com/niklashenning/pyqttooltip), pinned to commit [`c73d16404b2621e5f4a2faba8755a9c26c10135c`](https://github.com/niklashenning/pyqttooltip/tree/c73d16404b2621e5f4a2faba8755a9c26c10135c).

Copyright (c) 2024 Niklas Henning. The complete MIT notice is in [LICENSE](LICENSE).

`desktop_pet/_vendor/pyqttooltip_geometry.py` adapts the four cardinal branches of `PlacementUtils.__get_tooltip_rect` from `src/pyqttooltip/placement_utils.py`. It uses direct PySide6 imports and global logical `QRectF`/`QSizeF` coordinates, and corrects the upstream LEFT/RIGHT vertical centering calculation to use height. No additional runtime package is required.

`desktop_pet/bubble_placement.py` calls that adaptation and adds diagonal candidates, visible-character and face geometry, work-area clamping, collision priorities, and stable per-utterance variation. It evaluates tied candidates independently instead of copying the upstream AUTO dictionary keyed by free space. The speech card rendering, proportional typography and pagination are implemented by AURA.
