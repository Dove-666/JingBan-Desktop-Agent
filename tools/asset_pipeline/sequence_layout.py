"""One shared transform for a segmented clip; never per-frame auto-cropping."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class CanvasLayout:
    width: int = 1024
    height: int = 1024
    safe_extent: int = 896
    anchor_x: int = 512
    anchor_y: int = 960
    alpha_threshold: int = 1

    @classmethod
    def load(cls, path: Path) -> "CanvasLayout":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        canvas = data["canvas"]
        anchor = data["bottom_anchor_pixels"]
        result = cls(canvas["width"], canvas["height"], data["safe_extent"],
                     anchor[0], anchor[1], data.get("layout_alpha_threshold", 1))
        if (result.width != 1024 or result.height != 1024 or
                not 0 < result.safe_extent < min(result.width, result.height) or
                not 0 < result.anchor_x < result.width or
                not 0 < result.anchor_y < result.height or
                result.anchor_y - result.safe_extent < 1):
            raise ValueError("Invalid 1024 canvas or safe placement settings")
        return result


def visible_bounds(image: Image.Image, threshold: int = 1):
    if image.mode != "RGBA":
        raise ValueError("Each segmented frame must be RGBA")
    return image.getchannel("A").point(lambda value: 255 if value > threshold else 0).getbbox()


def inspect_sequence(paths: list[Path], layout: CanvasLayout):
    if not paths:
        raise ValueError("No segmented frames supplied")
    union = None
    size = None
    bounds = []
    for path in paths:
        with Image.open(path) as frame:
            if size is None:
                size = frame.size
            if frame.size != size:
                raise ValueError("All source frames must have identical dimensions")
            box = visible_bounds(frame, layout.alpha_threshold)
            if box is None:
                raise ValueError(f"Empty SAM2 mask at frame {len(bounds) + 1}")
            bounds.append(list(box))
            union = (box if union is None else
                     (min(union[0], box[0]), min(union[1], box[1]),
                      max(union[2], box[2]), max(union[3], box[3])))
    return size, union, bounds


def normalize_sequence(paths: list[Path], destination: Path,
                       layout: CanvasLayout = CanvasLayout(), *,
                       union_paths: list[Path] | None = None,
                       placement: dict | None = None) -> dict:
    """Keep all original frame canvases and use one scale/translation for the clip."""
    size, union, source_bounds = inspect_sequence(union_paths or paths, layout)
    if not paths:
        raise ValueError("No output frames supplied")
    if union_paths is not None and not set(paths).issubset(set(union_paths)):
        raise ValueError("Delivered frames must be selected from the full segmented sequence")
    left, top, right, bottom = union
    scale = (min(layout.safe_extent / (right - left), layout.safe_extent / (bottom - top))
             if placement is None else placement.get("scale"))
    if not isinstance(scale, (int, float)) or not 0 < scale < 100:
        raise ValueError("Invalid fixed sequence scale")
    if placement is not None and placement.get("source_size") != list(size):
        raise ValueError("Fixed placement belongs to a different source canvas")
    resized_size = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
    sx, sy = resized_size[0] / size[0], resized_size[1] / size[1]
    if placement is None:
        position = (round(layout.anchor_x - (left + right) * sx / 2),
                    round(layout.anchor_y - bottom * sy))
    else:
        reference = placement.get("reference_bbox")
        if (not isinstance(reference, list) or len(reference) != 4 or
                not 0 <= reference[0] < reference[2] <= size[0] or
                not 0 <= reference[1] < reference[3] <= size[1]):
            raise ValueError("Fixed placement requires a valid character standing reference box")
        position = (round(layout.anchor_x - (reference[0] + reference[2]) * sx / 2),
                    round(layout.anchor_y - reference[3] * sy))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    frame_reports = []
    area_previous = None
    largest_area_ratio = 1.0
    for index, path in enumerate(paths, 1):
        with Image.open(path) as opened:
            resized = opened.resize(resized_size, Image.Resampling.LANCZOS)
        frame = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 0))
        frame.alpha_composite(resized, position)
        alpha = frame.getchannel("A")
        box = visible_bounds(frame, layout.alpha_threshold)
        # Check actual nonzero alpha as well as layout bounds, including thin hair.
        actual = alpha.getbbox()
        if actual is None or actual[0] < 1 or actual[1] < 1 or actual[2] >= layout.width or actual[3] >= layout.height:
            raise ValueError(f"Clipped or empty alpha at normalized frame {index}")
        histogram = alpha.histogram()
        area = sum(histogram[layout.alpha_threshold + 1:])
        if area_previous:
            largest_area_ratio = max(largest_area_ratio, area / area_previous, area_previous / area)
        area_previous = area
        frame_reports.append({"index": index, "visible_bounds": list(box),
                              "alpha_bounds": list(actual), "visible_pixels": area,
                              "soft_edge_pixels": sum(histogram[1:255])})
        frame.save(destination / f"{index:04d}.png", format="PNG", optimize=True)
    return {"source_size": list(size), "union_bbox": list(union),
            "union_source_frame_count": len(union_paths or paths),
            "source_frame_bounds": source_bounds, "scale": scale,
            "resized_canvas": list(resized_size), "translation": list(position),
            "canvas": {"width": layout.width, "height": layout.height},
            "anchor": {"x": layout.anchor_x / layout.width, "y": layout.anchor_y / layout.height},
            "shared_transform": True, "frame_count": len(paths),
            "placement_mode": "standing_reference_shared_scale" if placement is not None else "clip_union_fit",
            "largest_adjacent_alpha_area_ratio": round(largest_area_ratio, 4),
            "warnings": (["Large mask-area change; inspect for tracking loss or legitimate occlusion"]
                         if largest_area_ratio > 1.65 else []),
            "frames": frame_reports,
            "limitations": "Integrity/alpha checks only; edge halos, character identity and action consistency require visual review"}
