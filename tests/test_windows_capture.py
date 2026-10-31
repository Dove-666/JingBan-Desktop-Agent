"""Pure geometry/pixel and mocked WinAPI failure tests, never desktop capture."""

from contextlib import nullcontext
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

from desktop_pet.perception import windows_capture as capture


def colored_image(width=100, height=100):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#2359a0"))
    return image


class CaptureGeometryTests(unittest.TestCase):
    def test_intersection_respects_negative_coordinates_and_empty_edges(self):
        self.assertEqual(capture.intersect_rect((-50, -30, 80, 90), (-20, -50, 40, 60)),
                         (-20, -30, 40, 60))
        for first, second in (((0, 0, 10, 10), (10, 0, 20, 10)),
                              ((0, 0, 10, 10), (0, 10, 10, 20)),
                              ((0, 0, 10, 10), (20, 20, 30, 30))):
            with self.subTest(first=first, second=second):
                self.assertIsNone(capture.intersect_rect(first, second))

    def test_single_monitor_clips_to_visible_target_only(self):
        self.assertEqual(capture.select_visible_rect((-30, -20, 200, 120), [(0, 0, 1920, 1080)]),
                         (0, 0, 200, 120))
        self.assertEqual(capture.select_visible_rect((100, 150, 200, 250), [(0, 0, 1920, 1080)]),
                         (100, 150, 200, 250))

    def test_negative_monitor_origin_is_preserved(self):
        monitors = [(-1280, -200, 0, 824), (0, 0, 1920, 1080)]
        self.assertEqual(capture.select_visible_rect((-1500, -300, -100, 700), monitors),
                         (-1280, -200, -100, 700))

    def test_virtual_desktop_gaps_and_offscreen_regions_are_not_captured(self):
        monitors = [(0, 0, 800, 600), (1000, 0, 1800, 600)]
        for window in ((810, 10, 990, 400), (-500, 100, -10, 400), (0, 700, 400, 900)):
            with self.subTest(window=window):
                self.assertIsNone(capture.select_visible_rect(window, monitors))
        self.assertIsNone(capture.select_visible_rect((0, 0, 100, 100), []))

    def test_spanning_window_uses_largest_monitor_piece_without_filling_gap(self):
        monitors = [(0, 0, 800, 600), (1000, 0, 1800, 600)]
        result = capture.select_visible_rect((750, -100, 1300, 700), monitors)
        self.assertEqual(result, (1000, 0, 1300, 600))
        self.assertNotEqual(result, (750, 0, 1300, 600))


class OverlayPrivacyTests(unittest.TestCase):
    def test_mask_intersection_does_not_change_other_pixels_or_image_size(self):
        image = colored_image(80, 60)
        result = capture.mask_overlays(image, (-20, 30, 60, 90), [(-30, 20, 0, 50)])
        self.assertEqual((result.width(), result.height()), (80, 60))
        self.assertEqual(result.pixelColor(0, 0), QColor("#808080"))
        self.assertEqual(result.pixelColor(19, 19), QColor("#808080"))
        self.assertEqual(result.pixelColor(20, 19), QColor("#2359a0"))
        self.assertEqual(result.pixelColor(19, 20), QColor("#2359a0"))
        self.assertEqual(result.pixelColor(79, 59), QColor("#2359a0"))

    def test_overlapping_masks_form_a_union_without_hiding_uncovered_pixels(self):
        result = capture.mask_overlays(colored_image(), (0, 0, 100, 100),
                                       [(0, 0, 40, 100), (20, 0, 70, 100), (20, 0, 70, 100)])
        self.assertEqual(result.pixelColor(69, 50), QColor("#808080"))
        self.assertEqual(result.pixelColor(70, 50), QColor("#2359a0"))
        self.assertEqual((result.width(), result.height()), (100, 100))

    def test_outside_and_edge_touching_overlays_do_not_modify_target(self):
        image = colored_image(80, 60)
        before = image.copy()
        result = capture.mask_overlays(image, (100, 200, 180, 260),
                                       [(0, 0, 100, 260), (180, 200, 250, 260), (120, 260, 170, 300)])
        self.assertEqual(result, before)

    def test_union_of_overlays_covering_the_target_is_rejected(self):
        for overlays in ([(0, 0, 100, 100)],
                         [(0, 0, 50, 100), (40, 0, 100, 100)],
                         [(-50, -50, 150, 150)]):
            with self.subTest(overlays=overlays):
                with self.assertRaisesRegex(capture.CaptureUnavailable, "完全被其他窗口遮挡"):
                    capture.mask_overlays(colored_image(), (0, 0, 100, 100), overlays)


class CaptureFlowTests(unittest.TestCase):
    def mocked_capture(self, image, bounds=(100, 100, 200, 200), monitors=None,
                       overlays=None, foreground=None, final_bounds=None):
        user = SimpleNamespace(GetForegroundWindow=Mock(side_effect=foreground or [42, 42]))
        patches = [
            patch.object(capture.sys, "platform", "win32"),
            patch.object(capture, "_api", return_value=(user, object(), object())),
            patch.object(capture, "_physical_coordinates", return_value=nullcontext()),
            patch.object(capture, "_bounds", side_effect=[bounds, final_bounds or bounds]),
            patch.object(capture, "_monitors", return_value=monitors or [(0, 0, 3000, 2000)]),
            patch.object(capture, "_overlays", side_effect=overlays or [[], []]),
            patch.object(capture, "_copy_region", return_value=image),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        return mocks[-1]

    def test_capture_reads_only_clipped_region_and_masks_before_and_after_overlays(self):
        copy_region = self.mocked_capture(colored_image(), bounds=(-30, 100, 100, 200),
                                         overlays=[[(0, 100, 30, 200)], [(20, 100, 60, 200)]])
        image = capture.capture_visible_window(42)
        self.assertEqual(copy_region.call_args.args[0], (0, 100, 100, 200))
        self.assertEqual(image.pixelColor(59, 50), QColor("#808080"))
        self.assertEqual(image.pixelColor(60, 50), QColor("#2359a0"))
        self.assertEqual((image.width(), image.height()), (100, 100))

    def test_large_target_preview_preserves_ratio_after_region_capture(self):
        copy_region = self.mocked_capture(colored_image(1600, 800), bounds=(100, 100, 1700, 900))
        image = capture.capture_visible_window(42)
        self.assertEqual(copy_region.call_args.args[0], (100, 100, 1700, 900))
        self.assertEqual((image.width(), image.height()), (640, 320))

    def test_target_changed_before_capture_is_not_read(self):
        copy_region = self.mocked_capture(colored_image(), foreground=[99])
        with self.assertRaisesRegex(capture.CaptureUnavailable, "不是前台窗口"):
            capture.capture_visible_window(42)
        copy_region.assert_not_called()

    def test_foreground_or_bounds_change_during_capture_discards_the_frame(self):
        for arguments in ({"foreground": [42, 99]}, {"final_bounds": (101, 100, 201, 200)}):
            with self.subTest(arguments=arguments):
                self.mocked_capture(colored_image(), **arguments)
                with self.assertRaisesRegex(capture.CaptureUnavailable, "截图期间窗口已移动或切换"):
                    capture.capture_visible_window(42)
                # Release mocks here, before starting this loop's next independent setup.
                self.doCleanups()

    def test_black_target_cannot_be_validated_by_a_bright_overlay(self):
        black = colored_image()
        black.fill(QColor("black"))
        for y in range(black.height()):
            for x in range(40):
                black.setPixelColor(x, y, QColor("white"))
        self.mocked_capture(black, overlays=[[(100, 100, 140, 200)], []])
        with self.assertRaisesRegex(capture.CaptureUnavailable, "近乎全黑"):
            capture.capture_visible_window(42)


class RegionResourceTests(unittest.TestCase):
    def test_oversized_region_is_rejected_without_acquiring_any_screen_dc(self):
        user, gdi = Mock(), Mock()
        with self.assertRaises(capture.CaptureUnavailable):
            capture._copy_region((0, 0, 9000, 9000), user, gdi)
        user.GetDC.assert_not_called()
        gdi.CreateCompatibleDC.assert_not_called()

    def test_failed_memory_dc_still_releases_screen_dc(self):
        user, gdi = Mock(), Mock()
        user.GetDC.return_value = 21
        gdi.CreateCompatibleDC.return_value = 0
        with self.assertRaisesRegex(capture.CaptureUnavailable, "缓冲"):
            capture._copy_region((-5, 10, 15, 40), user, gdi)
        user.GetDC.assert_called_once_with(None)
        user.ReleaseDC.assert_called_once_with(None, 21)
        gdi.DeleteObject.assert_not_called()

    def test_failed_region_dib_cleans_up_and_requests_only_target_dimensions(self):
        user, gdi = Mock(), Mock()
        user.GetDC.return_value = 21
        gdi.CreateCompatibleDC.return_value = 23
        header_values = {}

        def fail_dib(_dc, info, *_args):
            header = ctypes.cast(info, ctypes.POINTER(capture._BitmapInfo)).contents.header
            header_values.update(width=header.biWidth, height=header.biHeight, bits=header.biBitCount)
            return 0

        gdi.CreateDIBSection.side_effect = fail_dib
        with self.assertRaisesRegex(capture.CaptureUnavailable, "缓冲"):
            capture._copy_region((-5, 10, 15, 40), user, gdi)
        self.assertEqual(header_values, {"width": 20, "height": -30, "bits": 32})
        gdi.DeleteDC.assert_called_once_with(23)
        user.ReleaseDC.assert_called_once_with(None, 21)
        gdi.BitBlt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
