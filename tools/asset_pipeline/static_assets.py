"""Deterministic canvas placement of existing RGBA art, never image generation.

Only scale and translate the complete source canvas. Pixels and soft alpha are
preserved; a tiny-alpha threshold affects layout measurement only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


def normalize_static(source: Path, destination: Path, *, canvas=1024,
                     safe_extent=896, bottom_anchor=960, layout_alpha=1):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Keep the original source; choose a separate output path")
    with Image.open(source) as opened:
        if opened.mode != "RGBA":
            raise ValueError("Source must have a real RGBA alpha channel")
        image = opened.copy()
    alpha = image.getchannel("A")
    bounds = alpha.point(lambda value: 255 if value > layout_alpha else 0).getbbox()
    if bounds is None:
        raise ValueError("Source contains no visible character")
    left, top, right, bottom = bounds
    scale = min(safe_extent / (right - left), safe_extent / (bottom - top))
    size = (round(image.width * scale), round(image.height * scale))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    scaled_bounds = resized.getchannel("A").point(lambda value: 255 if value > layout_alpha else 0).getbbox()
    # Measure after resampling: Lanczos can extend soft edges by a few pixels.
    # Do not crop those edges or let them shift the visible bottom anchor.
    position = (round(canvas / 2 - (scaled_bounds[0] + scaled_bounds[2]) / 2), bottom_anchor - scaled_bounds[3])
    output = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    output.alpha_composite(resized, position)
    measured = output.getchannel("A").point(lambda value: 255 if value > layout_alpha else 0).getbbox()
    if measured is None or measured[0] < 1 or measured[1] < 1 or measured[2] >= canvas or measured[3] >= canvas:
        raise ValueError("Normalized character would touch or exceed the canvas")
    destination.parent.mkdir(parents=True, exist_ok=True)
    output.save(destination, format="PNG", optimize=True)
    return {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "source_size": list(image.size), "source_visible_bounds": list(bounds),
        "canvas": {"width": canvas, "height": canvas},
        "scale": scale, "translation": list(position),
        "visible_bounds": list(measured), "bottom_anchor": bottom_anchor,
        "layout_alpha_threshold": layout_alpha,
        "operation": "full-canvas uniform scale and translation; alpha retained",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    options = parser.parse_args()
    print(json.dumps(normalize_static(options.source, options.destination), indent=2))


if __name__ == "__main__":
    main()
