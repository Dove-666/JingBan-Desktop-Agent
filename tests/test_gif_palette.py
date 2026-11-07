"""Brief mouth colors survive encoding in a blue-dominant character preview."""

from io import BytesIO
import unittest

from PIL import Image, ImageDraw

from tools.asset_pipeline.gif_palette import quantize_animation


class GifPaletteTests(unittest.TestCase):
    @staticmethod
    def character_frames():
        frames = []
        for index in range(12):
            image = Image.new("RGB", (96, 96), (28, 30, 36))
            pixels = image.load()
            for y in range(8, 90):
                for x in range(10, 86):
                    pixels[x, y] = (20 + x // 3, 45 + y // 2,
                                    min(250, 110 + (x + y) // 2))
            draw = ImageDraw.Draw(image)
            draw.ellipse((29, 22, 67, 64), fill=(245, 210, 200))
            draw.line((43, 53, 53, 53), fill=(30, 30, 40))
            if index in (4, 5):
                draw.rectangle((43, 48, 53, 56), fill=(210, 75, 95))
            # Keep GIF frames distinct, so decoding preserves chronological IDs.
            draw.rectangle((0, 0, 4, 4), fill=(index * 20, index * 20, index * 20))
            frames.append(image)
        return frames

    def test_transient_red_mouth_survives_actual_gif_encoding(self):
        frames = self.character_frames()
        original = (210, 75, 95)
        # Establish that this fixture reproduces the former single-frame bug.
        old_palette = frames[0].quantize(colors=256)
        old = frames[4].quantize(palette=old_palette).convert("RGB").getpixel((48, 52))
        self.assertLess(old[0] - old[1], 20)
        indexed = quantize_animation(frames, focus_boxes=((41, 46, 56, 59),))
        encoded = BytesIO()
        indexed[0].save(encoded, format="GIF", save_all=True,
                        append_images=indexed[1:], duration=[80] * len(indexed),
                        loop=0, disposal=1, optimize=False)
        encoded.seek(0)
        with Image.open(encoded) as gif:
            self.assertEqual(gif.n_frames, len(frames))
            for index in (4, 5):
                gif.seek(index)
                red = gif.convert("RGB").getpixel((48, 52))
                self.assertGreater(red[0] - red[1], 100)
                self.assertLess(max(abs(a - b) for a, b in zip(red, original)), 12)

    def test_all_frames_contribute_without_changing_source_or_palette_per_frame(self):
        blue = Image.new("RGB", (32, 32), (25, 70, 180))
        pink = Image.new("RGB", (32, 32), (218, 70, 110))
        frames = [blue, blue.copy(), pink]
        before = [image.tobytes() for image in frames]
        indexed = quantize_animation(frames)
        self.assertEqual(indexed[-1].convert("RGB").getpixel((16, 16)), (218, 70, 110))
        self.assertTrue(all(image.getpalette() == indexed[0].getpalette() for image in indexed))
        self.assertEqual([image.tobytes() for image in frames], before)
        self.assertEqual([image.size for image in indexed], [(32, 32)] * 3)

    def test_invalid_viewing_frames_and_focus_boxes_are_rejected(self):
        rgb = Image.new("RGB", (16, 16))
        for images in ([], [rgb, Image.new("RGB", (8, 8))], [rgb.convert("RGBA")]):
            with self.subTest(images=len(images)):
                with self.assertRaises(ValueError):
                    quantize_animation(images)
        for box in ((0, 0, 17, 16), (4, 4, 3, 8), (0, 0, 16.0, 16)):
            with self.subTest(box=box):
                with self.assertRaises(ValueError):
                    quantize_animation([rgb], focus_boxes=(box,))
        with self.assertRaises(ValueError):
            quantize_animation([rgb], colors=True)


if __name__ == "__main__":
    unittest.main()
