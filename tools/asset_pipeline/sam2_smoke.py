"""Isolated real-SAM2 API check using repeated existing art, NOT an action video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from tools.asset_pipeline.sam2_video import segment_frames, sha256
from tools.asset_pipeline.sequence_layout import CanvasLayout, normalize_sequence


def run_smoke(source: Path, checkpoint: Path, output: Path, canvas: Path) -> dict:
    if output.exists():
        raise ValueError("Use a new smoke output directory")
    output.mkdir(parents=True)
    rgb, jpeg = output / "fixture_rgb", output / "fixture_jpeg"
    rgb.mkdir()
    jpeg.mkdir()
    original_hash = sha256(source)
    with Image.open(source) as opened:
        character = opened.convert("RGBA")
    reference = character.getchannel("A")
    box = reference.point(lambda value: 255 if value > 24 else 0).getbbox()
    if box is None:
        raise ValueError("Reference artwork has no visible alpha")
    background = Image.new("RGBA", character.size, (210, 215, 222, 255))
    background.alpha_composite(character)
    # Four identical composites test actual temporal propagation, not motion quality.
    for index in range(4):
        background.convert("RGB").save(rgb / f"{index:05d}.png")
        background.convert("RGB").save(jpeg / f"{index:05d}.jpg", quality=98, subsampling=0)
    started = time.monotonic()
    try:
        segmentation = segment_frames(rgb, jpeg, output / "segmented", checkpoint=checkpoint, box=list(box))
        qa = normalize_sequence(sorted((output / "segmented").glob("*.png")),
                                output / "normalized", CanvasLayout.load(canvas))
        target = np.asarray(reference) > 128
        overlaps = []
        for path in sorted((output / "segmented").glob("*.png")):
            with Image.open(path) as frame:
                actual = np.asarray(frame.getchannel("A")) > 128
            overlaps.append(round(float(np.logical_and(actual, target).sum() /
                                        np.logical_or(actual, target).sum()), 6))
        report = {"status": "passed", "synthetic": True, "actual_action_video": False,
                  "fixture": "four repeated original-art composites on neutral background; no motion generated",
                  "sam2": segmentation, "normalization": qa, "reference_alpha_iou": overlaps,
                  "duration_seconds": round(time.monotonic() - started, 3),
                  "original_unchanged": sha256(source) == original_hash,
                  "product_animation_ready": False,
                  "limitations": ["No actual requested action video exists",
                                  "Tracking on motion, cross-action size and visual edge quality remain unverified"]}
    except Exception as error:
        report = {"status": "failed", "synthetic": True, "actual_action_video": False,
                  "error_type": type(error).__name__, "error": str(error),
                  "original_unchanged": sha256(source) == original_hash,
                  "product_animation_ready": False}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Inspection sheet on light/dark backdrops, still a synthetic technical artifact.
    with Image.open(output / "normalized" / "0001.png") as normalized:
        preview = Image.new("RGB", (1024, 512))
        thumbnail = normalized.resize((512, 512), Image.Resampling.LANCZOS)
        for offset, color in ((0, (245, 245, 245)), (512, (35, 43, 59))):
            panel = Image.new("RGBA", (512, 512), color + (255,))
            panel.alpha_composite(thumbnail)
            preview.paste(panel.convert("RGB"), (offset, 0))
        preview.save(output / "synthetic-edge-preview.png")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("assets/whale.png"))
    parser.add_argument("--checkpoint", type=Path, default=Path("artifacts/asset_pipeline/checkpoints/sam2.1_hiera_tiny.pt"))
    parser.add_argument("--canvas", type=Path, default=Path("assets/character/canvas.json"))
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    report = run_smoke(options.source, options.checkpoint, options.output, options.canvas)
    print(json.dumps({key: report[key] for key in ("status", "synthetic", "actual_action_video", "reference_alpha_iou", "duration_seconds", "original_unchanged", "product_animation_ready")}, indent=2))


if __name__ == "__main__":
    main()
