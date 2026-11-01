"""Typing/resize regressions through the shipped offline Qt WebEngine page.

The real renderer receives Qt keyboard events; all conversations and drafts use
an in-memory test controller. No application settings, credentials, clipboard,
real conversation archive, model requests, or running desktop pet are touched.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from PySide6.QtTest import QTest

from tests import test_assistant_ui as assistant_fixture
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js
from tests.test_glass_ui import GlassInteractions


class TypingStabilityTests(GlassInteractions, unittest.TestCase):
    setUpClass = assistant_fixture.AssistantUiTests.__dict__["setUpClass"]
    tearDown = assistant_fixture.AssistantUiTests.tearDown

    def setUp(self):
        assistant_fixture.AssistantUiTests.setUp(self)
        self.panel.activateWindow()
        self.wait_for_renderer_size(self.panel)
        # Wait for the initial composer measurement and bridge history refresh
        # before observing changes attributable to an actual keyboard edit.
        wait_js(self.panel, "document.querySelector('#message-input').style.height !== ''")
        QTest.qWait(100)

    def editor_metrics(self):
        return eval_js(self.panel, """(() => {
            const editor=document.querySelector('#message-input');
            return {height:editor.getBoundingClientRect().height,
                clientHeight:editor.clientHeight,scrollHeight:editor.scrollHeight,
                width:editor.getBoundingClientRect().width,value:editor.value,
                overflow:getComputedStyle(editor).overflowY};
        })()""")

    def test_default_composer_and_menu_use_static_material_without_backdrop_layers(self):
        self.open_row_menu(self.panel)
        result = eval_js(self.panel, """(() => {
            const problems=[];
            for (const selector of ['#message-input','.row-popover button']) {
                for (let node=document.querySelector(selector);node;node=node.parentElement) {
                    const style=getComputedStyle(node);
                    for (const key of ['filter','backdropFilter','webkitBackdropFilter']) {
                        if (style[key] && style[key] !== 'none')
                            problems.push({node:node.className,key,value:style[key]});
                    }
                }
            }
            return {problems,renderers:[...document.querySelectorAll('.glass-surface')]
                .map(node=>node.dataset.glassRenderer),
                definitions:document.querySelectorAll('.glass-surface-definitions').length};
        })()""")
        self.assertEqual(result["problems"], [], "Editable text must not live in a backdrop-filter layer")
        self.assertEqual(result["renderers"], ["static", "static"])
        self.assertEqual(result["definitions"], 0)
        self.click_web(self.panel, "[data-testid=row-action-rename]")
        wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")

    def test_continuous_single_line_typing_does_not_rewrite_visible_editor_height(self):
        set_dom_text(self.panel, "#message-input", "a")
        self.click_web(self.panel, "#message-input")
        initial_height = self.editor_metrics()["height"]
        load_started = Mock()
        self.panel.web_view.loadStarted.connect(load_started)
        eval_js(self.panel, """(() => {
            const editor=document.querySelector('#message-input');
            window.qaTyping={styleMutations:[],focusLosses:0,editor};
            window.qaTypingObserver=new MutationObserver(records=>{
                window.qaTyping.styleMutations.push(...records.map(record=>record.oldValue));
            });
            window.qaTypingObserver.observe(editor,{attributes:true,
                attributeFilter:['style'],attributeOldValue:true});
            editor.addEventListener('blur',()=>{window.qaTyping.focusLosses+=1;});
            return true;
        })()""")
        typed = " steady keyboard input"
        QTest.keyClicks(self.keyboard_target(self.panel), typed, delay=4)
        wait_js(self.panel, "document.querySelector('#message-input').value === 'a steady keyboard input'")
        QTest.qWait(100)
        result = eval_js(self.panel, """(() => {
            window.qaTypingObserver.disconnect();
            return {mutations:window.qaTyping.styleMutations,
                focusLosses:window.qaTyping.focusLosses,
                sameEditor:window.qaTyping.editor===document.querySelector('#message-input'),
                focused:document.activeElement===window.qaTyping.editor};
        })()""")
        self.assertEqual(result["mutations"], [], "Same-line edits should not toggle height to auto and back")
        self.assertEqual(result["focusLosses"], 0)
        self.assertTrue(result["sameEditor"])
        self.assertTrue(result["focused"])
        self.assertAlmostEqual(self.editor_metrics()["height"], initial_height, delta=1)
        load_started.assert_not_called()
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.panel.web_state()["drafts"]["current"], "a" + typed)

    def test_multiline_paste_grows_caps_and_shrinks_after_deletion(self):
        set_dom_text(self.panel, "#message-input", "short")
        single = self.editor_metrics()
        set_dom_text(self.panel, "#message-input", "first line\nsecond line\nthird line")
        wait_js(self.panel, "document.querySelector('#message-input').getBoundingClientRect().height > 60")
        multiline = self.editor_metrics()
        self.assertGreater(multiline["height"], single["height"] + 20)
        self.assertLessEqual(multiline["height"], 160)
        self.assertLessEqual(multiline["scrollHeight"], multiline["clientHeight"] + 1)

        pasted = "\n".join("A pasted line " + str(index) for index in range(80))
        set_dom_text(self.panel, "#message-input", pasted)
        wait_js(self.panel, "document.querySelector('#message-input').scrollHeight > 160")
        capped = self.editor_metrics()
        self.assertLessEqual(capped["height"], 160)
        self.assertGreaterEqual(capped["height"], 155)
        self.assertGreater(capped["scrollHeight"], capped["clientHeight"])
        self.assertEqual(capped["overflow"], "auto")

        set_dom_text(self.panel, "#message-input", "short")
        wait_js(self.panel, "document.querySelector('#message-input').getBoundingClientRect().height < 50")
        shrunk = self.editor_metrics()
        self.assertAlmostEqual(shrunk["height"], single["height"], delta=1)
        self.assertEqual(shrunk["value"], "short")
        self.assertEqual(self.chat.sent, [])

    def test_unchanged_draft_remeasures_when_viewport_width_changes(self):
        self.panel.resize(1160, 720)
        self.wait_for_renderer_size(self.panel)
        draft = "Whale " * 12
        set_dom_text(self.panel, "#message-input", draft)
        QTest.qWait(100)
        wide = self.editor_metrics()
        self.panel.resize(420, 720)
        self.wait_for_renderer_size(self.panel)
        wait_js(self.panel, "document.querySelector('#message-input').getBoundingClientRect().height > "
                + str(wide["height"] + 10))
        narrow = self.editor_metrics()
        self.assertLess(narrow["width"], wide["width"])
        self.assertGreater(narrow["height"], wide["height"])
        self.assertLessEqual(narrow["height"], 160)
        self.assertEqual(narrow["value"], draft)
        self.panel.resize(1160, 720)
        self.wait_for_renderer_size(self.panel)
        wait_js(self.panel, "document.querySelector('#message-input').getBoundingClientRect().height < "
                + str(narrow["height"] - 10))
        restored = self.editor_metrics()
        self.assertAlmostEqual(restored["height"], wide["height"], delta=1)
        self.assertEqual(restored["value"], draft)

    def test_typing_keeps_long_history_dom_scroll_and_focus_stable(self):
        self.chat.messages = [
            {"id": f"qa-stable-{index}", "role": "assistant" if index % 2 else "user",
             "source": "assistant" if index % 2 else "user",
             "content": f"History message {index}\n\n" + "Keep this reading position. " * 8}
            for index in range(48)
        ]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('[data-message-id]').length === 48")
        set_dom_text(self.panel, "#message-input", "a")
        self.click_web(self.panel, "#message-input")
        eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            viewport.scrollTop=Math.round((viewport.scrollHeight-viewport.clientHeight)*.35);
            viewport.dispatchEvent(new Event('scroll'));
            return true;
        })()""")
        QTest.qWait(100)
        load_started = Mock()
        self.panel.web_view.loadStarted.connect(load_started)
        eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            window.qaHistory={viewport,scrollTop:viewport.scrollTop,
                messages:[...document.querySelectorAll('[data-message-id]')],
                markdown:[...document.querySelectorAll('.markdown')],mutations:0};
            window.qaHistoryObserver=new MutationObserver(records=>{
                window.qaHistory.mutations+=records.length;
            });
            window.qaHistoryObserver.observe(document.querySelector('.reading-column'),
                {subtree:true,childList:true,characterData:true});
            return true;
        })()""")
        QTest.keyClicks(self.keyboard_target(self.panel), " stable history edit", delay=4)
        wait_js(self.panel, "document.querySelector('#message-input').value === 'a stable history edit'")
        QTest.qWait(100)
        result = eval_js(self.panel, """(() => {
            window.qaHistoryObserver.disconnect();
            const old=window.qaHistory;
            const messages=[...document.querySelectorAll('[data-message-id]')];
            const markdown=[...document.querySelectorAll('.markdown')];
            return {mutations:old.mutations,scrollDelta:Math.abs(old.viewport.scrollTop-old.scrollTop),
                sameMessages:messages.length===old.messages.length &&
                    messages.every((node,index)=>node===old.messages[index]),
                sameMarkdown:markdown.length===old.markdown.length &&
                    markdown.every((node,index)=>node===old.markdown[index]),
                focused:document.activeElement===document.querySelector('#message-input')};
        })()""")
        self.assertEqual(result["mutations"], 0)
        self.assertLessEqual(result["scrollDelta"], 1)
        self.assertTrue(result["sameMessages"])
        self.assertTrue(result["sameMarkdown"])
        self.assertTrue(result["focused"])
        self.assertEqual(self.chat.sent, [])
        load_started.assert_not_called()


if __name__ == "__main__":
    unittest.main()
