"""Actual shipped WebEngine editor geometry after a reading-font change."""
import unittest

from PySide6.QtTest import QTest

from tests import test_assistant_ui as fixture
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js


class ComposerFontReflowTests(unittest.TestCase):
    setUpClass = fixture.AssistantUiTests.__dict__["setUpClass"]
    setUp = fixture.AssistantUiTests.setUp
    tearDown = fixture.AssistantUiTests.tearDown

    def metrics(self):
        return eval_js(self.panel, """(() => {
          const node = document.querySelector('#message-input');
          return {client: node.clientHeight, scroll: node.scrollHeight,
            text: node.value, same: node === window.qaFontEditor,
            font: getComputedStyle(node).fontSize};
        })()""")

    def change_font(self, size):
        self.chat.reading_font_size = size
        self.panel._queue_web_state()
        wait_js(self.panel, "getComputedStyle(document.querySelector('#message-input')).fontSize === '" + str(size) + "px'")
        QTest.qWait(50)

    def test_existing_draft_reflows_after_font_change_without_typing(self):
        text = "鲸鱼娘，保留尚未发送的草稿。\n第二行有中文和 emoji 🐳。"
        set_dom_text(self.panel, '#message-input', text)
        eval_js(self.panel, "(() => { window.qaFontEditor = document.querySelector('#message-input'); return true; })()")
        original = self.metrics()
        self.assertEqual(original['scroll'], original['client'])
        self.change_font(24)
        larger = self.metrics()
        self.assertEqual(larger['text'], text)
        self.assertTrue(larger['same'])
        self.assertEqual(larger['scroll'], larger['client'], "Changing the font must not clip a short existing draft")
        self.assertGreater(larger['client'], original['client'])
        self.change_font(12)
        smaller = self.metrics()
        self.assertEqual(smaller['scroll'], smaller['client'])
        self.assertLess(smaller['client'], larger['client'])
        self.assertEqual(smaller['text'], text)
        self.assertEqual(self.chat.sent, [])

    def test_long_draft_keeps_bounded_scroll_and_returns_to_short_height(self):
        text = '\n'.join('这是第 %d 行 🐳' % index for index in range(30))
        set_dom_text(self.panel, '#message-input', text)
        eval_js(self.panel, "(() => { window.qaFontEditor = document.querySelector('#message-input'); return true; })()")
        self.change_font(24)
        actual = self.metrics()
        self.assertEqual(actual['client'], 160)
        self.assertGreater(actual['scroll'], actual['client'])
        self.assertEqual(actual['text'], text)
        set_dom_text(self.panel, '#message-input', '短草稿')
        self.assertLess(self.metrics()['client'], 160)
        self.assertEqual(self.metrics()['scroll'], self.metrics()['client'])


if __name__ == '__main__':
    unittest.main()
