"""Streaming previews in the shipped Qt WebEngine using synthetic host state."""
import unittest
from unittest.mock import Mock, patch

from PySide6.QtTest import QTest

from tests import test_assistant_ui as fixture
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js


class ChatStreamUiTests(unittest.TestCase):
    setUpClass = fixture.AssistantUiTests.__dict__["setUpClass"]
    tearDown = fixture.AssistantUiTests.tearDown

    def setUp(self):
        fixture.AssistantUiTests.setUp(self)
        self.preview = None
        self.theme = "dark"
        self.running = False
        original = self.panel.web_state
        override = patch.object(self.panel, "web_state", side_effect=lambda: {
            **original(), "theme": self.theme, "streamingReply": self.preview,
            "busy": self.running, "requestInCurrent": self.running})
        override.start()
        self.addCleanup(override.stop)
        self.publish()
        wait_js(self.panel, "document.documentElement.dataset.theme === 'dark'")

    def publish(self, text=None, state="streaming", **changes):
        if text is not None:
            self.preview = {"conversationId": "current", "requestId": "local-qa-1", "text": text, "state": state, **changes}
        self.panel._queue_web_state()

    def test_incremental_text_reuses_one_bubble_and_keeps_editor_static(self):
        set_dom_text(self.panel, "#message-input", "保留我的草稿")
        eval_js(self.panel, """(() => {
          window.qaStreamEditor = document.querySelector('#message-input');
          window.qaStreamStyleWrites = 0;
          window.qaStreamObserver = new MutationObserver(rows => window.qaStreamStyleWrites += rows.length);
          window.qaStreamObserver.observe(window.qaStreamEditor, {attributes:true, attributeFilter:['style']});
          return true;
        })()""")
        loaded = Mock()
        self.panel.web_view.loadStarted.connect(loaded)
        self.running = True
        self.publish("你好")
        wait_js(self.panel, "document.querySelector('[data-stream-state=streaming] .markdown')?.textContent === '你好'")
        eval_js(self.panel, "(() => { window.qaStreamBubble = document.querySelector('[data-stream-state]'); return true; })()")
        for text in ("你好，鲸鱼", "你好，鲸鱼🐳。\n\n**正在逐字显示。**"):
            self.publish(text)
            wait_js(self.panel, "document.querySelector('[data-stream-state=streaming] .markdown')?.textContent.includes('" + text.split("\n")[0] + "')")
            QTest.qWait(60)
        result = eval_js(self.panel, """(() => {
          window.qaStreamObserver.disconnect();
          return {bubbles:document.querySelectorAll('[data-stream-state]').length,
            sameBubble:window.qaStreamBubble === document.querySelector('[data-stream-state]'),
            sameEditor:window.qaStreamEditor === document.querySelector('#message-input'),
            draft:window.qaStreamEditor.value, styleWrites:window.qaStreamStyleWrites,
            busy:document.querySelector('[data-stream-state]').getAttribute('aria-busy'),
            spinner:Boolean(document.querySelector('.streaming-indicator .spin')),
            theme:document.documentElement.dataset.theme,
            filter:getComputedStyle(document.querySelector('.glass-composer')).filter};
        })()""")
        self.assertEqual(result, {"bubbles": 1, "sameBubble": True, "sameEditor": True,
            "draft": "保留我的草稿", "styleWrites": 0, "busy": "true", "spinner": True,
            "theme": "dark", "filter": "none"})
        self.assertEqual(self.chat.sent, [])
        loaded.assert_not_called()

    def test_interrupted_text_stays_visible_and_success_replaces_preview_once(self):
        self.publish("这是一段已经收到的部分回复。")
        wait_js(self.panel, "Boolean(document.querySelector('[data-stream-state=streaming]'))")
        self.publish("这是一段已经收到的部分回复。", state="interrupted")
        wait_js(self.panel, "document.querySelector('[data-stream-state=interrupted]')?.textContent.includes('部分回复 · 未完成')")
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.streaming-indicator').length"), 0)
        self.assertNotIn("completed", eval_js(self.panel, "document.querySelector('[data-stream-state]').outerHTML"))
        self.chat.messages.append({"id": "final-reply", "role": "assistant", "content": "这是一段已经收到的部分回复。"})
        self.preview = None
        self.publish()
        wait_js(self.panel, "Boolean(document.querySelector('[data-message-id=final-reply]'))")
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('[data-stream-state]').length"), 0)
        self.assertEqual(eval_js(self.panel, "Array.from(document.querySelectorAll('.markdown')).filter(n => n.textContent === '这是一段已经收到的部分回复。').length"), 1)

    def test_stale_or_malformed_stream_preview_never_enters_another_conversation(self):
        for overrides in ({"conversationId": "other"}, {"requestId": "<unsafe>"}, {"state": "completed"}):
            self.publish("不应显示的旧回复", **overrides)
            QTest.qWait(150)
            self.assertFalse(eval_js(self.panel, "document.body.textContent.includes('不应显示的旧回复')"))
        self.publish("当前会话的一段回复")
        wait_js(self.panel, "Boolean(document.querySelector('[data-stream-state]'))")
        self.chat.open_conversation("old")
        wait_js(self.panel, "document.querySelector('.header-title')?.textContent === '旧会话'")
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('[data-stream-state]').length"), 0)


if __name__ == "__main__":
    unittest.main()
