"""Synthetic image quality tests; no screen or external service is accessed."""

import unittest

from PySide6.QtGui import QColor, QImage

from desktop_pet.perception.capture_quality import is_near_black


def solid_image(color, width=48, height=32):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


class CaptureQualityTests(unittest.TestCase):
    def test_null_and_uniform_black_or_near_black_are_unusable(self):
        self.assertTrue(is_near_black(QImage()))
        for color in ("#000000", "#080909", "#0c0c0c"):
            with self.subTest(color=color):
                self.assertTrue(is_near_black(solid_image(color)))

    def test_normal_dark_and_saturated_content_is_not_mistaken_for_black(self):
        for color in ("#181818", "#001828", "#002060", "#ffffff"):
            with self.subTest(color=color):
                self.assertFalse(is_near_black(solid_image(color)))

    def test_a_few_bright_artifacts_do_not_validate_an_otherwise_black_frame(self):
        image = solid_image("#000000")
        for x in range(16):
            image.setPixelColor(x, 0, QColor("white"))
        self.assertTrue(is_near_black(image))
        for x in range(32):
            image.setPixelColor(x, 0, QColor("white"))
        self.assertFalse(is_near_black(image))

    def test_quality_sampling_keeps_the_input_and_its_dimensions_unchanged(self):
        image = solid_image("#123456", 96, 64)
        before = image.copy()
        self.assertFalse(is_near_black(image))
        self.assertEqual(image.size(), before.size())
        self.assertEqual(image, before)

    def test_bright_or_gray_overlay_is_not_part_of_usable_content(self):
        image = solid_image("black")
        for y in range(32):
            for x in range(24):
                image.setPixelColor(x, y, QColor("#808080"))
        self.assertFalse(is_near_black(image))
        self.assertTrue(is_near_black(image, [(0, 0, 24, 32)]))
        self.assertTrue(is_near_black(image, [(0, 0, 48, 32)]))


if __name__ == "__main__":
    unittest.main()
