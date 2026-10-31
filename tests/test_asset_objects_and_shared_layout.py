"""Local synthetic geometry and prompt validation; does not create video or run SAM2."""

from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from tools.asset_pipeline.sam2_video import gate_effect_mask, save_alpha_frame, validate_effect_prompts
from tools.asset_pipeline.sequence_layout import normalize_sequence
from tools.asset_pipeline.shared_layout import plan_reference_placements


class EffectPromptTests(unittest.TestCase):
    def prompt(self, **updates):
        result = {"name": "question_mark", "frame_idx": 30, "start_frame": 20,
                  "end_frame": 90, "box": [100, 20, 140, 80]}
        result.update(updates)
        return result

    def test_real_effect_prompt_uses_independent_object_and_native_indices(self):
        result = validate_effect_prompts([self.prompt()], frame_count=150, width=512, height=512)
        self.assertEqual(result[0]["obj_id"], 2)
        self.assertEqual(result[0]["frame_idx"], 30)

    def test_default_does_not_invent_an_effect(self):
        self.assertEqual(validate_effect_prompts(None, frame_count=150, width=512, height=512), [])

    def test_invalid_visibility_prompt_and_path_name_rejected(self):
        for updates in ({"start_frame": 40}, {"end_frame": 150}, {"frame_idx": True},
                        {"name": "../question"}, {"name": "character"}, {"box": [100, 20, 999, 80]}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_effect_prompts([self.prompt(**updates)], frame_count=150, width=512, height=512)

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ValueError):
            validate_effect_prompts([self.prompt(), self.prompt()], frame_count=150, width=512, height=512)

    def test_question_curve_and_dot_can_have_separate_positive_points(self):
        result = validate_effect_prompts([self.prompt(points=[[115, 30], [120, 70]], labels=[1, 1])],
                                         frame_count=150, width=512, height=512)
        self.assertEqual(result[0]["labels"], [1, 1])
        with self.assertRaises(ValueError):
            validate_effect_prompts([self.prompt(points=[[115, 30]], labels=[])],
                                    frame_count=150, width=512, height=512)

    def test_invisible_frames_never_retain_a_hallucinated_effect(self):
        mask = Image.new("L", (30, 30), 0)
        ImageDraw.Draw(mask).rectangle((5, 5, 10, 10), fill=255)
        effect = self.prompt(start_frame=20, end_frame=40)
        self.assertIsNone(gate_effect_mask(mask, effect, 19).getbbox())
        self.assertEqual(gate_effect_mask(mask, effect, 20).tobytes(), mask.tobytes())
        self.assertEqual(gate_effect_mask(mask, effect, 40).tobytes(), mask.tobytes())
        self.assertIsNone(gate_effect_mask(mask, effect, 41).getbbox())
        self.assertIsNotNone(mask.getbbox())

    def test_fast_encoding_preserves_soft_edges_and_clears_only_invisible_rgb(self):
        with tempfile.TemporaryDirectory() as temporary:
            rgb = Image.new("RGBA", (3, 1), (50, 100, 150, 255))
            alpha = Image.new("L", (3, 1))
            alpha.putdata([0, 35, 255])
            path = Path(temporary) / "frame.png"
            save_alpha_frame(rgb, alpha, path)
            with Image.open(path) as result:
                self.assertEqual(list(result.getdata()), [(0, 0, 0, 0), (50, 100, 150, 35), (50, 100, 150, 255)])
            self.assertEqual(list(rgb.getdata()), [(50, 100, 150, 255)] * 3)


class SharedReferenceLayoutTests(unittest.TestCase):
    def clips(self):
        return {"first": {"source_size": [300, 300], "reference_bbox": [100, 50, 200, 250],
                          "union_bbox": [80, 40, 220, 250]},
                "second": {"source_size": [300, 300], "reference_bbox": [125, 150, 175, 250],
                           "union_bbox": [115, 140, 185, 250]}}

    def test_different_source_standing_sizes_deliver_same_height(self):
        plan = plan_reference_placements(self.clips())
        a, b = plan["placements"]["first"], plan["placements"]["second"]
        self.assertAlmostEqual(200 * a["scale"], 100 * b["scale"])
        self.assertAlmostEqual(b["scale"], 2 * a["scale"])
        self.assertTrue(plan["visual_review_required"])

    def test_question_mark_changes_common_safety_not_reference_height(self):
        clips = self.clips()
        before = plan_reference_placements(clips)
        clips["second"]["union_bbox"] = [10, 0, 290, 250]
        after = plan_reference_placements(clips)
        self.assertLess(after["common_safety_factor"], before["common_safety_factor"])
        for name in clips:
            self.assertEqual(after["placements"][name]["standing_base_scale"],
                             before["placements"][name]["standing_base_scale"])
            self.assertLess(after["placements"][name]["scale"], before["placements"][name]["scale"])

    def test_reference_pose_not_action_bbox_preserves_scale_for_crouch(self):
        clips = self.clips()
        # The second clip has the same recorded standing reference, while later
        # action frames can be much lower. Nothing derives height from crouching.
        plan = plan_reference_placements(clips)
        self.assertEqual(plan["placements"]["second"]["reference_bbox"], [125, 150, 175, 250])
        self.assertAlmostEqual(plan["placements"]["second"]["standing_base_scale"], 8.96)

    def test_two_real_clips_required_before_joint_claim(self):
        with self.assertRaises(ValueError):
            plan_reference_placements({"first": self.clips()["first"]})

    def test_fixed_layout_aligns_feet_and_height_without_per_frame_refitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clips = self.clips()
            plan = plan_reference_placements(clips)
            heights = []
            for name, clip in clips.items():
                image = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
                left, top, right, bottom = clip["reference_bbox"]
                ImageDraw.Draw(image).rectangle((left, top, right - 1, bottom - 1), fill=(20, 80, 200, 255))
                path = root / f"{name}.png"
                image.save(path)
                qa = normalize_sequence([path], root / name, placement=plan["placements"][name])
                self.assertEqual(qa["placement_mode"], "standing_reference_shared_scale")
                # LANCZOS support can extend soft alpha on deliberately tiny
                # unit fixtures; standing alignment concerns the opaque body.
                with Image.open(root / name / "0001.png") as output:
                    box = output.getchannel("A").point(lambda value: 255 if value > 128 else 0).getbbox()
                self.assertLessEqual(abs(box[3] - 960), 2)
                heights.append(box[3] - box[1])
            self.assertLessEqual(abs(heights[0] - heights[1]), 2)


if __name__ == "__main__":
    unittest.main()
