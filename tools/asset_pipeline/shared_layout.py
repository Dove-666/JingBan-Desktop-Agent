"""Calibrate whole clips from standing character height; effects only affect safety."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from PIL import Image

from tools.asset_pipeline.sequence_layout import CanvasLayout, inspect_sequence, normalize_sequence, visible_bounds


def plan_reference_placements(clips: dict[str, dict], *, layout: CanvasLayout = CanvasLayout(),
                              target_reference_height: float = 896) -> dict:
    """One standing height across clips plus a common shrink factor for all motion/effects."""
    if len(clips) < 2 or not math.isfinite(target_reference_height) or target_reference_height <= 0:
        raise ValueError("At least two clips and a positive standing-height target are required")
    factor = 1.0
    candidates = {}
    # Keep 8px for filter support at the extreme bottom; normal standing feet stay y960.
    safe_left = (layout.width - layout.safe_extent) / 2
    safe_right = layout.width - safe_left
    safe_top = layout.anchor_y - layout.safe_extent
    safe_bottom = layout.height - 8
    for name, clip in clips.items():
        size, union, reference = clip.get("source_size"), clip.get("union_bbox"), clip.get("reference_bbox")
        if (not isinstance(size, list) or len(size) != 2 or any(type(value) is not int or value <= 0 for value in size)):
            raise ValueError("Every clip requires its actual source size")
        for bounds in (union, reference):
            if (not isinstance(bounds, list) or len(bounds) != 4 or
                    any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds) or
                    not 0 <= bounds[0] < bounds[2] <= size[0] or not 0 <= bounds[1] < bounds[3] <= size[1]):
                raise ValueError("Every clip requires valid union and character reference boxes")
        base_scale = target_reference_height / (reference[3] - reference[1])
        pivot_x, pivot_y = (reference[0] + reference[2]) / 2, reference[3]
        extents = ((pivot_x - union[0], layout.anchor_x - safe_left),
                   (union[2] - pivot_x, safe_right - layout.anchor_x),
                   (pivot_y - union[1], layout.anchor_y - safe_top),
                   (union[3] - pivot_y, safe_bottom - layout.anchor_y))
        for source_extent, available in extents:
            if source_extent > 0:
                factor = min(factor, max(0, available - 4) / (source_extent * base_scale))
        candidates[name] = {"source_size": size, "union_bbox": union, "reference_bbox": reference,
                            "standing_base_scale": base_scale}
    if factor <= 0:
        raise ValueError("Joint motion/effects do not fit the canvas")
    for placement in candidates.values():
        placement["scale"] = placement["standing_base_scale"] * factor
    return {"schema_version": 1, "canvas": {"width": layout.width, "height": layout.height},
            "anchor": {"x": layout.anchor_x / layout.width, "y": layout.anchor_y / layout.height},
            "target_standing_height_before_safety": target_reference_height,
            "common_safety_factor": factor, "delivered_standing_height": target_reference_height * factor,
            "placements": candidates,
            "policy": "character-only standing reference; all native-frame character+effect unions constrain one shared safety factor",
            "visual_review_required": True}


def measure_segmented_clip(directory: Path, *, reference_index: int = 0,
                           layout: CanvasLayout = CanvasLayout()) -> dict:
    directory = Path(directory)
    paths = sorted((directory / "source_rgba").glob("*.png"))
    size, union, _bounds = inspect_sequence(paths, layout)
    if not 0 <= reference_index < len(paths):
        raise ValueError("Standing reference index is outside the retained native sequence")
    # Explicit character layer excludes an independent floating question mark.
    character = directory / "object_layers" / "character" / f"{reference_index + 1:04d}.png"
    if not character.is_file():
        manifest = json.loads((directory / "animation.json").read_text(encoding="utf-8"))
        if manifest.get("origin", {}).get("effect_prompts"):
            raise ValueError("Character-only layer is missing; do not calibrate standing height using an effect")
        character = paths[reference_index]
    with Image.open(character) as image:
        reference = visible_bounds(image, layout.alpha_threshold)
    if reference is None:
        raise ValueError("Standing character reference has empty alpha")
    return {"source_size": list(size), "union_bbox": list(union), "reference_bbox": list(reference),
            "reference_native_index": reference_index}


def repack_with_shared_layout(source: Path, destination: Path, *, plan_path: Path,
                              name: str, canvas_path: Path, reviewed: bool = False) -> dict:
    """Repack retained real segmentation without another SAM2 request or overwriting it."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError("Output already exists")
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    placement = plan["placements"][name]
    manifest = json.loads((source / "animation.json").read_text(encoding="utf-8"))
    origin = manifest["origin"]
    paths = sorted((source / "source_rgba").glob("*.png"))
    if len(paths) != origin["source_frame_count"] or len(paths) != origin["source_frames_segmented"]:
        raise ValueError("Retained native RGBA sequence is incomplete")
    selected = origin["delivered_source_indices"]
    if (not isinstance(selected, list) or len(selected) != manifest["frame_count"] or len(selected) > 96 or
            any(type(index) is not int or not 0 <= index < len(paths) for index in selected)):
        raise ValueError("Invalid original delivery mapping")
    layout = CanvasLayout.load(canvas_path)
    if plan.get("canvas") != {"width": layout.width, "height": layout.height}:
        raise ValueError("Shared plan canvas does not match")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".shared-layout-", dir=destination.parent) as temporary:
        publish = Path(temporary) / "publish"
        publish.mkdir()
        qa = normalize_sequence([paths[index] for index in selected], publish / "frames", layout,
                                union_paths=paths, placement=placement)
        result = copy.deepcopy(manifest)
        result["origin"].update({"shared_layout_plan_sha256": hashlib.sha256(Path(plan_path).read_bytes()).hexdigest(),
                                  "shared_layout_name": name, "common_safety_factor": plan["common_safety_factor"],
                                  "visual_review_required": not reviewed, "source_rgba_retained": False,
                                  "segmented_source_manifest_sha256": hashlib.sha256((source / "animation.json").read_bytes()).hexdigest()})
        result["complete"] = bool(reviewed)
        result["qa_passed"] = not qa["warnings"]
        (publish / "animation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        qa["shared_layout"] = plan
        (publish / "qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
        publish.rename(destination)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canvas", type=Path, default=Path("assets/character/canvas.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--clip", nargs=3, metavar=("NAME", "RETAINED_ASSET_DIR", "STANDING_NATIVE_INDEX"), action="append", required=True)
    calibrate.add_argument("--reference-image", type=Path, default=Path("assets/character/master.png"))
    calibrate.add_argument("--output", type=Path, required=True)
    repack = commands.add_parser("repack")
    repack.add_argument("source", type=Path)
    repack.add_argument("destination", type=Path)
    repack.add_argument("--plan", type=Path, required=True)
    repack.add_argument("--name", required=True)
    repack.add_argument("--reviewed", action="store_true")
    options = parser.parse_args()
    if options.command == "calibrate":
        if options.output.exists():
            raise ValueError("Keep the existing calibration; use a new output path")
        layout = CanvasLayout.load(options.canvas)
        clips = {name: measure_segmented_clip(Path(directory), reference_index=int(index), layout=layout)
                 for name, directory, index in options.clip}
        if len(clips) != len(options.clip):
            raise ValueError("Duplicate clip name")
        with Image.open(options.reference_image) as reference_image:
            reference = visible_bounds(reference_image, layout.alpha_threshold)
        if reference is None:
            raise ValueError("Master has no visible character")
        plan = plan_reference_placements(clips, layout=layout, target_reference_height=reference[3] - reference[1])
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(plan, indent=2))
    else:
        result = repack_with_shared_layout(options.source, options.destination, plan_path=options.plan,
                                          name=options.name, canvas_path=options.canvas, reviewed=options.reviewed)
        print(json.dumps({"frame_count": result["frame_count"], "complete": result["complete"]}, indent=2))


if __name__ == "__main__":
    main()
