"""Real Meta SAM2 video segmentation, then whole-clip transparent canvas placement.

No video/image generation and no chroma-key replacement. The original input is
read-only. All outputs are staged and published only after complete validation.
"""

from __future__ import annotations

import argparse
import bisect
from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from PIL import Image, ImageFilter

from tools.asset_pipeline.sequence_layout import CanvasLayout, normalize_sequence


def validate_effect_prompts(prompts, *, frame_count: int, width: int, height: int) -> list[dict]:
    """Explicit prompts for effects actually present in source video, never synthesis."""
    if prompts is None:
        return []
    if not isinstance(prompts, list) or len(prompts) > 3:
        raise ValueError("Effects must be a list containing at most three source objects")
    validated = []
    names = set()
    for index, prompt in enumerate(prompts, 2):
        if not isinstance(prompt, dict):
            raise ValueError("Effect prompt must be an object")
        name = prompt.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) or name in names or name == "character":
            raise ValueError("Effect names must be unique safe identifiers")
        names.add(name)
        first, last, frame = prompt.get("start_frame"), prompt.get("end_frame"), prompt.get("frame_idx")
        if (any(type(value) is not int for value in (first, last, frame)) or
                not 0 <= first <= frame <= last < frame_count):
            raise ValueError("Effect visible interval and prompt frame must use valid zero-based native indices")
        box = prompt.get("box")
        if (not isinstance(box, list) or len(box) != 4 or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in box) or
                not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
            raise ValueError("Effect box must lie inside its source frame")
        item = {"obj_id": index, "name": name, "frame_idx": frame,
                "start_frame": first, "end_frame": last, "box": list(box)}
        if "points" in prompt:
            points, labels = prompt["points"], prompt.get("labels")
            if (not isinstance(points, list) or not 1 <= len(points) <= 32 or
                    any(not isinstance(point, list) or len(point) != 2 or
                        any(type(value) not in (int, float) or not math.isfinite(value) for value in point) or
                        not 0 <= point[0] < width or not 0 <= point[1] < height for point in points) or
                    not isinstance(labels, list) or len(labels) != len(points) or
                    any(type(label) is not int or label not in (0, 1) for label in labels)):
                raise ValueError("Effect positive/negative points and labels must match and lie inside the frame")
            item.update({"points": points, "labels": labels})
        validated.append(item)
    return validated


def gate_effect_mask(mask: Image.Image, effect: dict, frame_index: int) -> Image.Image:
    """Discard tracking guesses when a reviewed source effect is not actually visible."""
    if mask.mode != "L":
        raise ValueError("Effect mask must be single-channel alpha")
    return (mask.copy() if effect["start_frame"] <= frame_index <= effect["end_frame"]
            else Image.new("L", mask.size, 0))


def save_alpha_frame(rgb: Image.Image, alpha: Image.Image, path: Path) -> None:
    """Keep visible RGB/alpha exact; discard invisible background and encode quickly."""
    frame = rgb.copy()
    frame.putalpha(alpha)
    frame.paste((0, 0, 0, 0), mask=alpha.point(lambda value: 255 if value == 0 else 0))
    frame.save(path, format="PNG", compress_level=3, optimize=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(arguments: list[str], *, timeout: int = 180):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{Path(arguments[0]).name} failed: {result.stderr[-1500:]}")
    return result.stdout


def decode_video(source: Path, directory: Path, fps: int) -> dict:
    if not 12 <= fps <= 24:
        raise ValueError("Animation FPS must be 12 through 24")
    metadata = json.loads(_run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height,avg_frame_rate,duration:format=duration:frame=best_effort_timestamp_time",
                                "-of", "json", str(source)]))
    streams = metadata.get("streams", [])
    duration = float((streams[0].get("duration") if streams else None) or
                     metadata.get("format", {}).get("duration", 0))
    if not streams or not math.isfinite(duration) or not 0 < duration <= 15:
        raise ValueError("Expected one short video, at most 15 seconds")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    if min(width, height) < 16 or max(width, height) > 4096:
        raise ValueError("Unsupported video resolution")
    png = directory / "rgb"
    jpg = directory / "jpeg"
    png.mkdir()
    jpg.mkdir()
    # Preserve every decoded original frame; delivery sampling happens AFTER SAM2.
    _run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-an", "-fps_mode", "passthrough",
          "-start_number", "0", str(png / "%05d.png")])
    paths = sorted(png.glob("*.png"))
    if not 2 <= len(paths) <= 900:
        raise ValueError("Expected 2 through 900 original decoded frames")
    rate = str(streams[0].get("avg_frame_rate", "0/1"))
    numerator, denominator = rate.split("/")
    source_fps = float(numerator) / float(denominator) if float(denominator) else 0
    if not math.isfinite(source_fps) or source_fps <= 0:
        source_fps = len(paths) / duration
    raw_times = [frame.get("best_effort_timestamp_time") for frame in metadata.get("frames", [])]
    timestamps_estimated = (len(raw_times) != len(paths) or any(value is None for value in raw_times))
    timestamps = ([index / source_fps for index in range(len(paths))] if timestamps_estimated
                  else [float(value) - float(raw_times[0]) for value in raw_times])
    if any(not math.isfinite(value) for value in timestamps) or any(b < a for a, b in zip(timestamps, timestamps[1:])):
        raise ValueError("Invalid or out-of-order source frame timestamps")
    for path in paths:
        with Image.open(path) as frame:
            frame.convert("RGB").save(jpg / f"{path.stem}.jpg", quality=98, subsampling=0)
    return {"rgb": png, "jpeg": jpg, "frame_count": len(paths),
            "duration_seconds": duration, "source_size": [width, height],
            "source_fps": source_fps, "timestamps": timestamps,
            "timestamps_estimated": timestamps_estimated}


def delivery_indices(timestamps: list[float], duration: float, fps: int,
                     *, max_frames: int = 96) -> list[int]:
    """Select nearest original frame at each delivery timestamp, never interpolate."""
    if (not timestamps or not math.isfinite(duration) or duration <= 0 or
            not 12 <= fps <= 24):
        raise ValueError("Invalid sampling parameters")
    # ffprobe commonly rounds seconds to six decimals. A 124/24-second
    # source is reported as 5.166667, whose 12 FPS count is 62.000004.
    count = math.ceil(duration * fps - 1e-4)
    if count > max_frames:
        raise ValueError(f"Delivery exceeds {max_frames} frames; use 12 FPS for a five-second action")
    selected = []
    for number in range(count):
        target = number / fps
        after = min(bisect.bisect_left(timestamps, target), len(timestamps) - 1)
        before = max(0, after - 1)
        selected.append(before if abs(timestamps[before] - target) < abs(timestamps[after] - target) else after)
    if len(set(selected)) != len(selected):
        raise ValueError("Source frame rate is lower than delivery rate; choose a lower delivery FPS")
    return selected


def segment_frames(rgb_directory: Path, jpeg_directory: Path, destination: Path,
                   *, checkpoint: Path, box: list[float], device: str = "cuda",
                   feather: float = 0.45, effect_prompts: list[dict] | None = None,
                   layer_destination: Path | None = None) -> dict:
    """Track the character and explicitly prompted real effects with actual SAM2."""
    import numpy as np
    import torch
    from sam2.build_sam import build_sam2_video_predictor

    rgb_paths = sorted(Path(rgb_directory).glob("*.png"))
    jpeg_paths = sorted(Path(jpeg_directory).glob("*.jpg"))
    if len(rgb_paths) < 2 or [p.stem for p in rgb_paths] != [p.stem for p in jpeg_paths]:
        raise ValueError("Matching RGB PNG and numeric JPEG sequence required")
    with Image.open(rgb_paths[0]) as first:
        width, height = first.size
    if (len(box) != 4 or not all(math.isfinite(value) for value in box) or
            not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
        raise ValueError("Prompt box must be inside the original first-frame dimensions")
    if not 0 <= feather <= 1:
        raise ValueError("Feather radius must be zero through one pixel")
    effects = validate_effect_prompts(effect_prompts, frame_count=len(rgb_paths), width=width, height=height)
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable; choose --device cpu explicitly")
    destination.mkdir(parents=True, exist_ok=False)
    if layer_destination is not None:
        layer_destination.mkdir(parents=True, exist_ok=False)
        for name in ["character"] + [effect["name"] for effect in effects]:
            (layer_destination / name).mkdir()
    predictor = build_sam2_video_predictor("configs/sam2.1/sam2.1_hiera_t.yaml", str(checkpoint),
                                           device=device, apply_postprocessing=False)
    autocast = (torch.autocast("cuda", dtype=torch.bfloat16)
                if device == "cuda" and torch.cuda.is_bf16_supported() else nullcontext())
    seen = set()
    mask_areas = {}
    effect_areas = {effect["name"]: {} for effect in effects}
    with torch.inference_mode(), autocast:
        state = predictor.init_state(str(jpeg_directory), offload_video_to_cpu=True,
                                     offload_state_to_cpu=True, async_loading_frames=False)
        predictor.add_new_points_or_box(state, frame_idx=0, obj_id=1,
                                        box=np.asarray(box, dtype=np.float32))
        for effect in effects:
            predictor.add_new_points_or_box(state, frame_idx=effect["frame_idx"], obj_id=effect["obj_id"],
                                            box=np.asarray(effect["box"], dtype=np.float32),
                                            points=np.asarray(effect["points"], dtype=np.float32) if "points" in effect else None,
                                            labels=np.asarray(effect["labels"], dtype=np.int32) if "labels" in effect else None)
        for index, object_ids, logits in predictor.propagate_in_video(state):
            object_ids = list(object_ids)
            if index in seen or not 0 <= index < len(rgb_paths) or set(object_ids) != {1} | {effect["obj_id"] for effect in effects}:
                raise RuntimeError("Unexpected SAM2 frame/object sequence")
            mask = (logits[object_ids.index(1), 0] > 0).cpu().numpy().astype("uint8") * 255
            if mask.shape != (height, width) or not mask.any():
                raise RuntimeError(f"SAM2 produced an empty or wrong-sized mask at frame {index}")
            mask_areas[index] = int(np.count_nonzero(mask))
            with Image.open(rgb_paths[index]) as opened:
                frame = opened.convert("RGBA")
            if frame.size != (width, height):
                raise RuntimeError("Video frame dimensions changed")
            layers = {"character": mask.copy()}
            for effect in effects:
                effect_mask = (logits[object_ids.index(effect["obj_id"]), 0] > 0).cpu().numpy().astype("uint8") * 255
                if effect_mask.shape != (height, width):
                    raise RuntimeError("SAM2 produced a wrong-sized effect mask")
                effect_mask = np.asarray(gate_effect_mask(Image.fromarray(effect_mask), effect, index))
                effect_areas[effect["name"]][index] = int(np.count_nonzero(effect_mask))
                layers[effect["name"]] = effect_mask
                mask = np.maximum(mask, effect_mask)
            if layer_destination is not None:
                for name, layer_mask in layers.items():
                    layer_alpha = Image.fromarray(layer_mask)
                    if feather:
                        layer_alpha = layer_alpha.filter(ImageFilter.GaussianBlur(feather))
                    save_alpha_frame(frame, layer_alpha, layer_destination / name / f"{index + 1:04d}.png")
            alpha = Image.fromarray(mask)
            if feather:
                alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
            save_alpha_frame(frame, alpha, destination / f"{index + 1:04d}.png")
            seen.add(index)
    if seen != set(range(len(rgb_paths))):
        raise RuntimeError("SAM2 did not return every decoded frame")
    ordered_areas = [mask_areas[index] for index in range(len(rgb_paths))]
    if any(not any(areas.values()) for areas in effect_areas.values()):
        raise RuntimeError("A prompted source effect was never segmented; inspect its prompt instead of fabricating it")
    largest_area_ratio = max([1.0] + [max(a / b, b / a) for a, b in zip(ordered_areas, ordered_areas[1:])])
    del predictor, state
    if device == "cuda":
        torch.cuda.empty_cache()
    return {"segmentation": "sam2.1", "config": "configs/sam2.1/sam2.1_hiera_t.yaml",
            "frame_count": len(seen), "device": device, "prompt_box": list(box),
            "feather_radius_pixels": feather, "mask_threshold_logits": 0,
            "native_mask_areas": ordered_areas,
            "effect_prompts": effects,
            "effect_native_mask_areas": {name: [areas[index] for index in range(len(rgb_paths))]
                                         for name, areas in effect_areas.items()},
            "layer_policy": "independent SAM2 objects, explicit visibility gate, alpha union for final frames",
            "transparent_rgb_policy": "zero-alpha RGB cleared only; all visible RGB and alpha retained exactly",
            "largest_native_adjacent_area_ratio": round(largest_area_ratio, 4),
            "cuda_connected_components_extension": False,
            "checkpoint_sha256": sha256(checkpoint)}


def process_video(source: Path, destination: Path, *, checkpoint: Path, box: list[float],
                  canvas_config: Path, name: str, fps: int = 12,
                  completion: str = "idle", device: str = "cuda",
                  keep_source_frames: bool = False, reviewed: bool = False,
                  effect_prompts: list[dict] | None = None) -> dict:
    source, destination, checkpoint = Path(source), Path(destination), Path(checkpoint)
    if not source.is_file() or not checkpoint.is_file():
        raise ValueError("Input video and official checkpoint must exist")
    if destination.exists():
        raise ValueError("Output already exists; choose a new directory to preserve existing assets")
    if completion not in {"idle", "hold_last"}:
        raise ValueError("Unsupported completion policy")
    layout = CanvasLayout.load(canvas_config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sam2-stage-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        decoded = decode_video(source, stage, fps)
        selected = delivery_indices(decoded["timestamps"], decoded["duration_seconds"], fps)
        segmented = stage / "segmented"
        layer_destination = stage / "object_layers" if effect_prompts else None
        origin = segment_frames(decoded["rgb"], decoded["jpeg"], segmented,
                                checkpoint=checkpoint, box=box, device=device,
                                effect_prompts=effect_prompts, layer_destination=layer_destination)
        if origin.get("largest_native_adjacent_area_ratio", 1) > 1.65:
            raise RuntimeError("Large native-frame mask-area change; correct prompts or inspect tracking before delivery")
        publish = stage / "publish"
        publish.mkdir()
        all_paths = sorted(segmented.glob("*.png"))
        qa = normalize_sequence([all_paths[index] for index in selected], publish / "frames", layout,
                                union_paths=all_paths)
        if qa["warnings"]:
            raise RuntimeError("Mask tracking requires visual review: " + "; ".join(qa["warnings"]))
        origin.update({"source_sha256": sha256(source), "synthetic": False,
                       "visual_review_required": not reviewed,
                       "source_fps": decoded["source_fps"],
                       "source_duration_seconds": decoded["duration_seconds"],
                       "source_frame_count": decoded["frame_count"],
                       "source_frames_segmented": len(all_paths),
                       "delivered_fps": fps, "delivered_frame_count": len(selected),
                       "sampling": "SAM2 every native source frame, then nearest-timestamp delivery selection; no interpolation",
                       "delivered_source_indices": selected,
                       "timestamps_estimated": decoded["timestamps_estimated"],
                       "source_rgba_retained": keep_source_frames})
        if keep_source_frames:
            # Retain every native-size alpha frame separately from the game sequence.
            segmented.rename(publish / "source_rgba")
            if layer_destination is not None:
                layer_destination.rename(publish / "object_layers")
        manifest = {"schema_version": 1, "name": name, "canvas": qa["canvas"],
                    "fps": fps, "frame_count": len(selected),
                    "duration_ms": round(len(selected) / fps * 1000),
                    "frames": [f"frames/{index:04d}.png" for index in range(1, len(selected) + 1)],
                    "anchor": qa["anchor"], "loop": False, "completion": completion,
                    "union_bbox": qa["union_bbox"], "origin": origin,
                    # Integrity checks do not certify identity/action/edge appearance.
                    # The player refuses a draft until someone explicitly reviews it.
                    "complete": bool(reviewed), "qa_passed": True}
        (publish / "animation.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (publish / "qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
        # Atomic directory rename on the same volume; staged files removed on failure.
        publish.rename(destination)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--box", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"), required=True)
    parser.add_argument("--canvas", type=Path, default=Path("assets/character/canvas.json"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--fps", type=int, choices=range(12, 25), default=12,
                        help="Delivered game FPS only; SAM2 processes EVERY original frame")
    parser.add_argument("--keep-source-frames", action="store_true", help="Also retain all native-size SAM2 RGBA frames")
    parser.add_argument("--reviewed", action="store_true",
                        help="Mark complete only AFTER manual character/edge/action review; otherwise a non-playable draft")
    parser.add_argument("--completion", choices=("idle", "hold_last"), default="idle")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--effects", type=Path,
                        help="JSON array of real effect boxes, native prompt indices and explicit visible intervals")
    options = parser.parse_args()
    report = process_video(options.source, options.destination, checkpoint=options.checkpoint,
                           box=options.box, canvas_config=options.canvas, name=options.name,
                           fps=options.fps, completion=options.completion, device=options.device,
                           keep_source_frames=options.keep_source_frames, reviewed=options.reviewed,
                           effect_prompts=json.loads(options.effects.read_text(encoding="utf-8")) if options.effects else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
