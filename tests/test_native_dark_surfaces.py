"""Native theme changes repaint trusted surfaces without changing content."""

from unittest.mock import Mock
import unittest

from PySide6.QtCore import QEvent, QRect
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.bubble import SpeechBubble
from desktop_pet.ui.chat_panel import ChatPanel
from desktop_pet.ui.theme import ThemeManager, color, theme_manager
from tests.test_debug_ui import FakeChat


class BubbleOwner(QWidget):
    sprite_rect = QRect(0, 0, 160, 200)


class NativeDarkSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        previous = theme_manager()
        if previous is not None:
            previous.dispose()
        self.palette = QPalette(self.app.palette())
        settings = Mock()
        settings.load_theme.return_value = "light"
        self.manager = ThemeManager(settings)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.manager.dispose()
        self.manager.deleteLater()
        self.app.setPalette(self.palette)
        self.app.processEvents()

    def test_bubble_switch_repaints_text_without_moving_or_restarting_reading(self):
        owner = BubbleOwner()
        owner.resize(160, 200)
        owner.show()
        self.widgets.append(owner)
        bubble = SpeechBubble(owner)
        source = "保留 #ffffff、white 和蓝色海浪。" * 70
        bubble.say(source, 30000, conversation_link={"conversation_id": "local-c", "message_id": "local-m"})
        self.assertGreater(len(bubble._pages), 1)
        bubble._next_page()
        bubble._ensure_text_layer()
        light = bubble._text_layer.copy()
        state = (bubble.geometry(), bubble._layout_key, bubble._page_index,
                 bubble._speech_sequence, bubble.hide_timer.timerId(), bubble._page_timer.timerId())
        self.manager.set_theme("dark", persist=False)
        self.assertIsNone(bubble._text_layer_key)
        bubble._ensure_text_layer()
        self.assertNotEqual(light, bubble._text_layer)
        self.assertEqual(state, (bubble.geometry(), bubble._layout_key, bubble._page_index,
                                bubble._speech_sequence, bubble.hide_timer.timerId(), bubble._page_timer.timerId()))
        self.assertEqual(bubble._text, source)
        self.assertEqual("".join(bubble._pages), source)
        image = bubble.grab().toImage()
        point = bubble._body_rect.center().toPoint()
        point.setY(round(bubble._body_rect.top() + 5))
        self.assertEqual(image.pixelColor(point), QColor(color("surface")))
        self.manager.set_theme("light", persist=False)
        bubble._ensure_text_layer()
        self.assertEqual(light, bubble._text_layer)

    def test_native_transcript_retains_literal_colors_and_escapes_html_on_switch(self):
        chat = FakeChat()
        literal = "#ffffff white #e9f3f9 <style>body{color:red}</style>"
        chat.messages = [{"role": "user", "content": literal},
                         {"role": "assistant", "content": "普通回复 #f5f7fb"}]
        panel = ChatPanel(chat)
        self.widgets.append(panel)
        panel.input.setPlainText("未发送的草稿 #ffffff")
        before = panel.transcript.toPlainText()
        light_html = panel.transcript.toHtml()
        self.assertIn(literal, before)
        self.assertIn('#e9f3f9', light_html)
        self.manager.set_theme("dark", persist=False)
        dark_html = panel.transcript.toHtml()
        self.assertEqual(panel.transcript.toPlainText(), before)
        self.assertEqual(panel.input.toPlainText(), "未发送的草稿 #ffffff")
        self.assertIn('bgcolor="#1e3248"', dark_html)
        self.assertIn('bgcolor="#192330"', dark_html)
        self.assertIn('color:#e6edf5', dark_html)
        self.assertIn('&lt;style&gt;', dark_html)
        self.manager.set_theme("light", persist=False)
        self.assertEqual(panel.transcript.toPlainText(), before)
        self.assertIn('bgcolor="#e9f3f9"', panel.transcript.toHtml())

    def test_execution_and_reference_text_are_not_recolored_as_source(self):
        chat = FakeChat()
        chat.messages = [{"id": "local-u", "role": "user", "content": "一个任务", "metadata": {
            "request_status": "failed", "image_id": "invalid", "selection_context": {
                "title": "原文 #ffffff", "text": "white #e9f3f9 <script>示例</script>"}}}]
        panel = ChatPanel(chat)
        self.widgets.append(panel)
        expected = panel.transcript.toPlainText()
        self.manager.set_theme("dark", persist=False)
        html = panel.transcript.toHtml()
        self.assertEqual(panel.transcript.toPlainText(), expected)
        self.assertIn("white #e9f3f9", expected)
        self.assertIn("color:#f2a49b", html)
        self.assertIn("color:#a7b5c8", html)
        trace = {"state": "failed", "duration_ms": 1000, "events": [
            {"kind": "tool", "elapsed_ms": 0, "label": "#ffffff", "detail": "white #e9f3f9"}]}
        panel._execution_expanded["trace"] = True
        rendered = panel._execution_html("trace", trace)
        self.assertIn("white #e9f3f9", rendered)
        self.assertIn("#ffffff", rendered)
        self.assertIn("color:#a7b5c8", rendered)


if __name__ == "__main__":
    unittest.main()
