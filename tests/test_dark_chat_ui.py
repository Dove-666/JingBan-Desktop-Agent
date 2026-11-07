"""Dark materials through the shipped Qt WebEngine and synthetic chat bridge.

The fixtures never read production preferences, capture the desktop, contact a
model, or access the user's clipboard. Theme changes use ordinary state updates.
"""

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from tests import test_assistant_ui as fixture
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js
from tests.test_glass_ui import GlassInteractions


class DarkChatUiTests(GlassInteractions, unittest.TestCase):
    setUpClass = fixture.AssistantUiTests.__dict__["setUpClass"]
    tearDown = fixture.AssistantUiTests.tearDown

    def setUp(self):
        fixture.AssistantUiTests.setUp(self)
        self.theme = "dark"
        original = self.panel.web_state
        replacement = patch.object(self.panel, "web_state", side_effect=lambda: {**original(), "theme": self.theme})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.panel._queue_web_state()
        wait_js(self.panel, "document.documentElement.dataset.theme === 'dark'")
        self.wait_for_renderer_size(self.panel)
        QTest.qWait(100)

    def test_live_theme_switch_keeps_editor_draft_focus_and_webengine(self):
        self.click_web(self.panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(self.panel), "draft through theme change")
        wait_js(self.panel, "document.querySelector('#message-input').value === 'draft through theme change'")
        eval_js(self.panel, "(() => {window.qaThemeEditor = document.querySelector('#message-input'); return true;})()")
        started = Mock()
        self.panel.web_view.loadStarted.connect(started)
        for theme in ("light", "dark"):
            self.theme = theme
            self.panel._queue_web_state()
            wait_js(self.panel, f"document.documentElement.dataset.theme === '{theme}'")
            self.assertEqual(eval_js(self.panel, "getComputedStyle(document.documentElement).colorScheme"), theme)
            self.assertTrue(eval_js(self.panel, "window.qaThemeEditor === document.querySelector('#message-input')"))
            self.assertTrue(eval_js(self.panel, "document.activeElement === window.qaThemeEditor"))
            self.assertEqual(eval_js(self.panel, "window.qaThemeEditor.value"), "draft through theme change")
        started.assert_not_called()
        self.assertEqual(self.chat.sent, [])

    def test_dark_typing_is_static_and_does_not_rewrite_root_theme(self):
        self.click_web(self.panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(self.panel), "a")
        QTest.qWait(100)
        eval_js(self.panel, """(() => {
            const root = document.documentElement, editor = document.querySelector('#message-input');
            window.qaDarkTyping = {root:0, editor:0};
            window.qaDarkRootObserver = new MutationObserver(records => window.qaDarkTyping.root += records.length);
            window.qaDarkEditorObserver = new MutationObserver(records => window.qaDarkTyping.editor += records.length);
            window.qaDarkRootObserver.observe(root, {attributes:true});
            window.qaDarkEditorObserver.observe(editor, {attributes:true, attributeFilter:['style']});
            return true;
        })()""")
        QTest.keyClicks(self.keyboard_target(self.panel), " stable night glass", delay=3)
        wait_js(self.panel, "document.querySelector('#message-input').value === 'a stable night glass'")
        result = eval_js(self.panel, """(() => {
            window.qaDarkRootObserver.disconnect(); window.qaDarkEditorObserver.disconnect();
            const filters = [];
            for (let node=document.querySelector('#message-input'); node; node=node.parentElement) {
                const style=getComputedStyle(node);
                for (const key of ['filter','backdropFilter','webkitBackdropFilter'])
                    if (style[key] && style[key] !== 'none') filters.push({node:node.className,key,value:style[key]});
            }
            return {...window.qaDarkTyping, filters,
                renderer:document.querySelector('.glass-composer').dataset.glassRenderer,
                definitions:document.querySelectorAll('.glass-surface-definitions').length};
        })()""")
        self.assertEqual(result, {"root": 0, "editor": 0, "filters": [], "renderer": "static", "definitions": 0})
        self.assertEqual(self.panel.web_state()["drafts"]["current"], "a stable night glass")

    def test_dark_portal_menu_dialog_and_narrow_layout_use_the_same_material(self):
        for width in (420, 1060):
            with self.subTest(width=width):
                self.panel.resize(width, 720)
                self.wait_for_renderer_size(self.panel)
                self.open_row_menu(self.panel)
                result = eval_js(self.panel, """(() => {
                    const menu=document.querySelector('.row-popover'), r=menu.getBoundingClientRect();
                    return {background:getComputedStyle(menu).backgroundColor,
                        text:getComputedStyle(menu.querySelector('button')).color,
                        fits:r.left>=-1 && r.right<=innerWidth+1 && r.top>=-1 && r.bottom<=innerHeight+1,
                        backdrop:getComputedStyle(menu).backdropFilter};
                })()""")
                self.assertEqual(result["background"], "rgb(26, 38, 53)")
                self.assertEqual(result["text"], "rgb(214, 228, 245)")
                self.assertTrue(result["fits"])
                self.assertEqual(result["backdrop"], "none")
                self.click_web(self.panel, "[data-testid=row-action-rename]")
                wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")
                self.assertEqual(eval_js(self.panel, "getComputedStyle(document.querySelector('.dialog-input')).backgroundColor"),
                                 "rgb(17, 28, 41)")
                QTest.keyClick(self.keyboard_target(self.panel), Qt.Key.Key_Escape)
                wait_js(self.panel, "!document.querySelector('dialog[open]')")

    def test_dark_markdown_surfaces_and_reading_contrast(self):
        self.chat.messages.append({"id": "qa-night-rich", "role": "assistant", "source": "assistant",
                                   "content": "## 夜间阅读\n\n**保持清晰**\n\n```python\nprint('AURA')\n```\n\n| 项目 | 状态 |\n| --- | --- |\n| 主题 | 已切换 |"})
        self.chat.messages_changed.emit()
        wait_js(self.panel, "Boolean(document.querySelector('.code-block'))")
        values = eval_js(self.panel, """(() => {
            const rgb = value => (value.match(/[\\d.]+/g)||[]).slice(0,3).map(Number);
            const luminance = value => rgb(value).map(n=>{const s=n/255;return s<=.04045?s/12.92:((s+.055)/1.055)**2.4;})
                .reduce((sum,n,index)=>sum+n*[.2126,.7152,.0722][index],0);
            const background=getComputedStyle(document.querySelector('.main-panel')).backgroundColor;
            const normal=getComputedStyle(document.querySelector('.markdown')).color;
            const muted=getComputedStyle(document.querySelector('.header-title')).color;
            const contrast = color => (Math.max(luminance(color),luminance(background))+.05)/(Math.min(luminance(color),luminance(background))+.05);
            return {normal:contrast(normal),muted:contrast(muted),
                heading:getComputedStyle(document.querySelector('.markdown h2')).color,
                code:getComputedStyle(document.querySelector('.code-block')).backgroundColor,
                toolbar:getComputedStyle(document.querySelector('.code-toolbar')).backgroundColor,
                table:getComputedStyle(document.querySelector('.markdown th')).backgroundColor};
        })()""")
        self.assertGreaterEqual(values["normal"], 7)
        self.assertGreaterEqual(values["muted"], 4.5)
        self.assertEqual(values["heading"], "rgb(230, 237, 245)")
        self.assertEqual(values["code"], "rgb(19, 29, 41)")
        self.assertEqual(values["toolbar"], "rgb(26, 38, 53)")
        self.assertEqual(values["table"], "rgb(27, 40, 57)")


if __name__ == "__main__":
    unittest.main()
