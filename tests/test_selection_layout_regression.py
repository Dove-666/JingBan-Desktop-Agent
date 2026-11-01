"""User-visible selection composer regressions for long page metadata.

These tests use synthetic text only. Visibility checks intersect the actual
scroll viewport, because QWidget.isVisible() also returns true for a child
that is completely outside a scroll area's visible region.
"""

from html import escape
import unittest
from unittest.mock import patch
from urllib.parse import quote

from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from desktop_pet.ui.selection_ask_dialog import SelectionAskDialog


class SelectionLayoutRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.context = {
            "text": "这是用户明确选中的正文。右键提问后，应先能确认自己的选区。",
            "title": "新闻网页标题与中文长标题" * 18,
            "url": "https://example.invalid/" + quote("鲸鱼娘新闻页面") * 20 + "/story",
            "page_text": "网页相关背景。" * 300,
        }
        self.dialog = SelectionAskDialog(self.context)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _visible_selection_height(self):
        viewport = self.dialog.context_scroll.viewport()
        preview = self.dialog.selection_preview
        preview_rect = QRect(preview.mapTo(viewport, QPoint()), preview.size())
        return viewport.rect().intersected(preview_rect).height()

    def test_long_page_metadata_does_not_hide_selection_before_any_scrolling(self):
        for width, height in ((540, 520), (400, 400)):
            with self.subTest(size=(width, height)):
                self.dialog.resize(width, height)
                self.dialog.context_scroll.verticalScrollBar().setValue(0)
                self.app.processEvents()
                self.assertGreaterEqual(
                    self._visible_selection_height(), 38,
                    "The selected text must be visible when the composer first opens; "
                    "a long URL or title must not consume its preview area.",
                )

    def test_url_display_is_compact_without_changing_submitted_source(self):
        self.dialog.resize(400, 400)
        self.app.processEvents()
        self.assertLessEqual(
            self.dialog.url_label.height(),
            self.dialog.url_label.fontMetrics().lineSpacing() * 2 + 10,
            "The source address must not become a paragraph of percent escapes.",
        )
        self.assertEqual(self.dialog.context.text, self.context["text"])
        self.assertTrue(self.dialog.context.url.startswith("https://example.invalid/"))
        self.assertIn(
            self.dialog.context.url,
            self.dialog.url_label.toolTip(),
            "The complete safe source URL should remain available without occupying the preview.",
        )

    def test_source_tooltips_escape_page_html_and_url_display_stays_single_line(self):
        title = '<img src="https://example.invalid/pixel"> & <b>合成标题</b>'
        url = (
            "https://example.invalid/" + quote("鲸鱼娘")
            + "/%0A%0D%09%3Cimg%20src%3Dx%3E%26text/raw&label"
        )
        self.dialog.set_context({"text": "合成选区", "title": title, "url": url})
        self.app.processEvents()
        self.assertIn(escape("来自：" + title), self.dialog.source_label.toolTip())
        self.assertNotIn("<img", self.dialog.source_label.toolTip())
        self.assertNotIn("<b>", self.dialog.source_label.toolTip())
        self.assertIn(escape(self.dialog.context.url), self.dialog.url_label.toolTip())
        self.assertEqual(self.dialog.url_label.textFormat(), Qt.TextFormat.PlainText)
        for control in ("\n", "\r", "\t"):
            self.assertNotIn(control, self.dialog.url_label.text())
        self.assertNotIn("%0A", self.dialog.url_label.text())
        self.assertEqual(self.dialog.context.url, url)

    def test_expanding_page_reference_keeps_input_and_send_in_dialog(self):
        self.dialog.resize(400, 400)
        self.dialog.page_toggle.setChecked(True)
        self.app.processEvents()
        for widget in (self.dialog.input, self.dialog.send_button):
            widget_rect = QRect(widget.mapTo(self.dialog, QPoint()), widget.size())
            self.assertTrue(
                self.dialog.rect().contains(widget_rect),
                f"{widget.objectName() or type(widget).__name__} must stay reachable.",
            )
        self.assertGreater(self.dialog.context_scroll.verticalScrollBar().maximum(), 0)

    def test_small_and_negative_coordinate_work_areas_contain_entire_native_frame(self):
        # Simulated work areas avoid changing the user's display configuration.
        # A negative origin represents a secondary display to the left/above
        # the primary display; native title-bar dimensions must be included.
        for area in (QRect(0, 0, 800, 600), QRect(-1000, -200, 800, 600)):
            with self.subTest(area=area.getRect()):
                self.dialog.resize(1400, 1100)
                self.dialog.move(area.right() + 100, area.bottom() + 100)
                self.app.processEvents()
                self.dialog._fit_to_available_geometry(area, center=True)
                QTest.qWait(20)
                self.app.processEvents()
                self.assertTrue(
                    area.contains(self.dialog.frameGeometry()),
                    f"Native frame {self.dialog.frameGeometry().getRect()} must fit "
                    f"inside the logical work area {area.getRect()}, including title bar.",
                )
                for widget in (self.dialog.input, self.dialog.send_button):
                    rect = QRect(widget.mapTo(self.dialog, QPoint()), widget.size())
                    self.assertTrue(self.dialog.rect().contains(rect))

    def test_clamping_again_preserves_an_already_accessible_user_position(self):
        area = QRect(-1000, -200, 800, 600)
        self.dialog.resize(440, 400)
        self.dialog._fit_to_available_geometry(area, center=True)
        QTest.qWait(20)
        self.app.processEvents()
        # Deliberately move away from the centered position, while remaining
        # inside the work area, as a user dragging an open composer would.
        frame = self.dialog.frameGeometry()
        frame.moveTopLeft(area.topLeft() + QPoint(23, 27))
        self.dialog.move(frame.topLeft())
        self.app.processEvents()
        expected = self.dialog.frameGeometry()
        self.assertTrue(area.contains(expected))
        self.dialog._fit_to_available_geometry(area, center=False)
        QTest.qWait(20)
        self.app.processEvents()
        self.assertEqual(self.dialog.frameGeometry(), expected)

    def test_fast_close_and_delete_does_not_run_a_callback_on_destroyed_dialog(self):
        with patch("sys.excepthook") as exception_hook:
            for _ in range(3):
                transient = SelectionAskDialog({"text": "仅用于快速关闭测试的合成选区"})
                transient.show()
                transient.deleteLater()
                # Explicitly destroy the C++ widget before the zero-delay
                # screen-fit callback would ordinarily run. isVisible()
                # alone cannot prove this destruction actually occurred.
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(isValid(transient))
                self.app.processEvents()
                QTest.qWait(10)
            self.assertEqual(
                exception_hook.call_count, 0,
                "Closing a new composer must cancel its pending placement "
                "callback instead of invoking a deleted PySide widget: "
                + repr(exception_hook.call_args_list),
            )


if __name__ == "__main__":
    unittest.main()
