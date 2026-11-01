"""Pure local layout/validation checks; no checkpoint downloads or model loading."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from tools.asset_pipeline.sam2_video import decode_video, delivery_indices, process_video
from tools.asset_pipeline.sequence_layout import CanvasLayout, normalize_sequence


class SequenceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def frame(self, name, bounds, *, size=(200, 200), mode="RGBA"):
        image = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0))
        if bounds:
            ImageDraw.Draw(image).rectangle(bounds, fill=(20, 40, 210, 255) if mode == "RGBA" else (20, 40, 210))
        path = self.directory / name
        image.save(path)
        return path

    def test_motion_preserved_by_one_union_transform(self):
        first = self.frame("one.png", (30, 50, 90, 150))
        second = self.frame("two.png", (60, 20, 120, 120))
        report = normalize_sequence([first, second], self.directory / "output")
        self.assertEqual(report["union_bbox"], [30, 20, 121, 151])
        self.assertTrue(report["shared_transform"])
        a, b = report["frames"]
        self.assertGreater(b["visible_bounds"][0], a["visible_bounds"][0])
        self.assertLess(b["visible_bounds"][1], a["visible_bounds"][1])
        for index in (1, 2):
            with Image.open(self.directory / "output" / f"{index:04d}.png") as image:
                self.assertEqual(image.size, (1024, 1024))
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertAlmostEqual(report["anchor"]["y"], .9375)

    def test_does_not_recenter_each_frame(self):
        paths = [self.frame("one.png", (5, 50, 55, 100)),
                 self.frame("two.png", (100, 50, 150, 100))]
        report = normalize_sequence(paths, self.directory / "output")
        first, second = [item["visible_bounds"] for item in report["frames"]]
        self.assertGreater(second[0] - first[0], 500)

    def test_unsampled_transient_frame_still_controls_union(self):
        paths = [self.frame("one.png", (50, 50, 100, 100)),
                 self.frame("two.png", (0, 0, 150, 150)),
                 self.frame("three.png", (50, 50, 100, 100))]
        report = normalize_sequence([paths[0], paths[2]], self.directory / "output", union_paths=paths)
        self.assertEqual(report["union_bbox"], [0, 0, 151, 151])
        self.assertEqual(report["union_source_frame_count"], 3)
        self.assertEqual(report["frame_count"], 2)

    def test_empty_frame_rejected_before_output_creation(self):
        paths = [self.frame("one.png", (30, 50, 90, 150)), self.frame("empty.png", None)]
        with self.assertRaisesRegex(ValueError, "Empty"):
            normalize_sequence(paths, self.directory / "output")
        self.assertFalse((self.directory / "output").exists())

    def test_mixed_dimensions_and_no_alpha_rejected(self):
        good = self.frame("one.png", (30, 50, 90, 150))
        wrong = self.frame("two.png", (30, 50, 90, 150), size=(300, 300))
        with self.assertRaisesRegex(ValueError, "identical dimensions"):
            normalize_sequence([good, wrong], self.directory / "output")
        opaque = self.frame("opaque.png", (30, 50, 90, 150), mode="RGB")
        with self.assertRaisesRegex(ValueError, "RGBA"):
            normalize_sequence([opaque], self.directory / "output")

    def test_existing_output_not_overwritten(self):
        source = self.frame("one.png", (30, 50, 90, 150))
        destination = self.directory / "output"
        destination.mkdir()
        (destination / "important.txt").write_text("keep")
        with self.assertRaises(FileExistsError):
            normalize_sequence([source], destination)
        self.assertEqual((destination / "important.txt").read_text(), "keep")

    def test_config_rejects_unsafe_anchor(self):
        config = self.directory / "canvas.json"
        config.write_text(json.dumps({"canvas": {"width": 1024, "height": 1024},
                                      "safe_extent": 896, "bottom_anchor_pixels": [512, 500]}))
        with self.assertRaisesRegex(ValueError, "Invalid"):
            CanvasLayout.load(config)

    def test_invalid_fps_never_executes_decoder(self):
        with patch("tools.asset_pipeline.sam2_video._run") as run:
            with self.assertRaisesRegex(ValueError, "FPS"):
                decode_video(self.directory / "source.mp4", self.directory, 60)
            run.assert_not_called()

    def test_video_failure_leaves_no_publish_or_stage(self):
        source = self.directory / "source.mp4"
        source.write_bytes(b"not a real video")
        checkpoint = self.directory / "checkpoint.pt"
        checkpoint.write_bytes(b"test")
        config = self.directory / "canvas.json"
        config.write_text(json.dumps({"canvas": {"width": 1024, "height": 1024},
                                      "safe_extent": 896, "bottom_anchor_pixels": [512, 960]}))
        with patch("tools.asset_pipeline.sam2_video.decode_video", side_effect=RuntimeError("bad decode")):
            with self.assertRaisesRegex(RuntimeError, "bad decode"):
                process_video(source, self.directory / "output", checkpoint=checkpoint,
                              box=[0, 0, 10, 10], canvas_config=config, name="test")
        self.assertFalse((self.directory / "output").exists())
        self.assertEqual(list(self.directory.glob(".sam2-stage-*")), [])

    def test_native_30fps_five_seconds_derives_60_frames(self):
        indices = delivery_indices([index / 30 for index in range(150)], 5, 12)
        self.assertEqual(len(indices), 60)
        self.assertEqual(len(set(indices)), 60)
        self.assertEqual(indices[0], 0)
        self.assertIn(indices[-1], (147, 148))

    def test_real_124_frame_24fps_duration_rounding_delivers_62_frames(self):
        selected = delivery_indices([index / 24 for index in range(124)], 5.166667, 12)
        self.assertEqual(len(selected), 62)
        self.assertEqual(selected[-1], 122)

    def test_24fps_five_seconds_rejected_by_memory_cap(self):
        with self.assertRaisesRegex(ValueError, "96 frames"):
            delivery_indices([index / 30 for index in range(150)], 5, 24)

    def test_delivery_does_not_invent_missing_source_frames(self):
        with self.assertRaisesRegex(ValueError, "lower than delivery"):
            delivery_indices([index / 5 for index in range(25)], 5, 12)

    def test_full_native_frames_segmented_before_delivery_and_draft_manifest(self):
        source = self.directory / "source.mp4"
        source.write_bytes(b"mock video; never decoded or treated as real SAM2 evidence")
        checkpoint = self.directory / "checkpoint.pt"
        checkpoint.write_bytes(b"mock")
        config = self.directory / "canvas.json"
        config.write_text(json.dumps({"canvas": {"width": 1024, "height": 1024},
                                      "safe_extent": 896, "bottom_anchor_pixels": [512, 960]}))

        def decode(_source, stage, _fps):
            rgb, jpeg = stage / "rgb", stage / "jpeg"
            rgb.mkdir()
            jpeg.mkdir()
            for index in range(30):
                image = Image.new("RGB", (100, 100), "gray")
                image.save(rgb / f"{index:05d}.png")
                image.save(jpeg / f"{index:05d}.jpg")
            return {"rgb": rgb, "jpeg": jpeg, "frame_count": 30,
                    "duration_seconds": 1, "source_fps": 30,
                    "timestamps": [index / 30 for index in range(30)], "timestamps_estimated": False}

        def segment(rgb, jpeg, destination, **_kwargs):
            self.assertEqual(len(list(rgb.glob("*.png"))), 30)
            self.assertEqual(len(list(jpeg.glob("*.jpg"))), 30)
            destination.mkdir()
            for index in range(30):
                image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
                ImageDraw.Draw(image).rectangle((20, 20, 80, 80), fill=(50, 80, 160, 255))
                image.save(destination / f"{index + 1:04d}.png")
            return {"segmentation": "mock (unit test only)", "frame_count": 30}

        with patch("tools.asset_pipeline.sam2_video.decode_video", side_effect=decode), patch(
                "tools.asset_pipeline.sam2_video.segment_frames", side_effect=segment):
            manifest = process_video(source, self.directory / "output", checkpoint=checkpoint,
                                     box=[0, 0, 90, 90], canvas_config=config, name="fixture",
                                     keep_source_frames=True)
        self.assertEqual(manifest["frame_count"], 12)
        self.assertEqual(manifest["origin"]["source_frames_segmented"], 30)
        self.assertEqual(manifest["origin"]["source_fps"], 30)
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["origin"]["visual_review_required"])
        self.assertEqual(len(list((self.directory / "output" / "source_rgba").glob("*.png"))), 30)
        self.assertEqual(len(list((self.directory / "output" / "frames").glob("*.png"))), 12)
        self.assertEqual(list(self.directory.glob(".sam2-stage-*")), [])


if __name__ == "__main__":
    unittest.main()
