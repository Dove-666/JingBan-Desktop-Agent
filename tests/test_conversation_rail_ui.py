"""Conversation ruler interactions through the shipped, real WebEngine page.

All messages and drafts are synthetic and memory-only. The tests never open a
production archive, settings file, clipboard, model connection or desktop feed.
"""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests import test_assistant_ui as fixture
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js
from tests.test_glass_ui import GlassInteractions


RAIL = "[data-testid=conversation-rail]"
TRACK = "[data-testid=conversation-rail-track]"
PREVIEW = "[data-testid=conversation-rail-preview]"


def synthetic_messages(count=48, prefix="rail"):
    return [
        {"id": f"{prefix}-{index:03d}",
         "role": "user" if index % 2 == 0 else "assistant",
         "source": "user" if index % 2 == 0 else "assistant",
         "content": f"第 {index + 1} 条合成对话\n\n" +
                    ("This is local reading material for conversation navigation. " * 5)}
        for index in range(count)
    ]


class ConversationRailUiTests(GlassInteractions, unittest.TestCase):
    setUpClass = fixture.AssistantUiTests.__dict__["setUpClass"]
    tearDown = fixture.AssistantUiTests.tearDown

    def setUp(self):
        self.chat = fixture.AssistantUiChat()
        self.chat.messages = synthetic_messages()
        self.opened = []
        self.extra_panels = []
        self.panel = fixture.make_panel(
            self.chat, settings_opener=lambda: self.opened.append("settings"))
        self.panel.activateWindow()
        self.wait_for_renderer_size(self.panel)
        self.theme = "light"
        original = self.panel.web_state
        replacement = patch.object(self.panel, "web_state",
                                   side_effect=lambda: {**original(), "theme": self.theme})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.panel._queue_web_state()
        wait_js(self.panel, f"Boolean(document.querySelector('{RAIL}'))")
        QTest.qWait(100)

    def publish_messages(self, messages):
        self.chat.messages = messages
        self.chat.messages_changed.emit()
        wait_js(self.panel, f"document.querySelectorAll('.message[data-message-id]').length === {len(messages)}")
        QTest.qWait(100)

    def point_on_track(self, index, count=None):
        count = len(self.chat.messages) if count is None else count
        rectangle = eval_js(self.panel, """(() => {
            const rail = document.querySelector(TRACK) || document.querySelector(RAIL);
            const r = rail.getBoundingClientRect();
            return {left:r.left, top:r.top, width:r.width, height:r.height};
        })()""".replace("TRACK", json.dumps(TRACK)).replace("RAIL", json.dumps(RAIL)))
        self.assertGreater(rectangle["width"], 0)
        self.assertGreater(rectangle["height"], 0)
        fraction = index / max(1, count - 1)
        # Stay one logical pixel inside the hit band at either end.
        y = rectangle["top"] + max(1, min(rectangle["height"] - 1,
                                         fraction * rectangle["height"]))
        return QPoint(round(rectangle["left"] + rectangle["width"] / 2), round(y))

    def hover_index(self, index):
        point = self.point_on_track(index)
        target = self.keyboard_target(self.panel)
        self.panel.web_view.setFocus(); target.setFocus()
        eval_js(self.panel, """(() => {
            window.qaRailPointerMoves=[];
            if (!window.qaRailPointerObserver) {
                window.qaRailPointerObserver=true;
                window.addEventListener('pointermove',event => {
                    window.qaRailPointerMoves.push({x:event.clientX,y:event.clientY,trusted:event.isTrusted,
                        rail:Boolean(event.target?.closest?.('[data-testid=conversation-rail]'))});
                },true);
            }
            return true;
        })()""")
        hit = eval_js(self.panel, """(() => {
            const hit=document.elementFromPoint(X,Y);
            return Boolean(hit?.closest('[data-testid=conversation-rail]'));
        })()""".replace("X",str(point.x())).replace("Y",str(point.y())))
        self.assertTrue(hit,"The native pointer must hit the visible rail")
        # Windows QTest.mouseMove may only reposition the OS cursor without
        # delivering a renderer event. Send targeted Qt input, without moving
        # the user's physical cursor or dispatching a JavaScript pointer event.
        for local in (QPoint(max(40,target.width()//2),12),target.mapFrom(self.panel.web_view,point)):
            event=QMouseEvent(QEvent.Type.MouseMove,QPointF(local),QPointF(target.mapToGlobal(local)),
                Qt.MouseButton.NoButton,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target,event);self.app.processEvents();QTest.qWait(35)
        wait_js(self.panel,"window.qaRailPointerMoves.some(event=>event.rail&&event.trusted)")
        wait_js(self.panel, f"document.querySelector('{PREVIEW}')?.dataset.previewMessageId === "
                + json.dumps(self.chat.messages[index]["id"]))

    def click_index(self, index):
        point = self.point_on_track(index)
        self.panel.web_view.setFocus()
        target = self.keyboard_target(self.panel)
        QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                         pos=target.mapFrom(self.panel.web_view, point))
        self.wait_target_visible(index)

    def wait_target_visible(self, index):
        selector = ".message[data-message-id=" + json.dumps(self.chat.messages[index]["id"]) + "]"
        wait_js(self.panel, """(() => {
            const node=document.querySelector(SELECTOR), viewport=document.querySelector('.thread-viewport');
            const r=node.getBoundingClientRect(), v=viewport.getBoundingClientRect();
            return r.bottom>v.top+8 && r.top<v.bottom-8;
        })()""".replace("SELECTOR", json.dumps(selector)))
        QTest.qWait(120)

    def wheel_on_track(self, angle_delta):
        """Deliver a native wheel event at the ruler, outside the scroll viewport."""
        point = self.point_on_track(len(self.chat.messages) // 2)
        target = self.keyboard_target(self.panel)
        local = target.mapFrom(self.panel.web_view, point)
        event = QWheelEvent(
            QPointF(local), QPointF(target.mapToGlobal(local)), QPoint(),
            QPoint(0, angle_delta), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(target, event)
        self.app.processEvents()

    def test_empty_and_loading_histories_do_not_leave_stale_ticks_or_previews(self):
        self.hover_index(10)
        self.chat.history_loading = True
        self.chat.history_busy_changed.emit(True)
        wait_js(self.panel, f"!document.querySelector('{RAIL}') && !document.querySelector('{PREVIEW}')")
        self.chat.history_loading = False
        self.chat.messages = []
        self.chat.history_busy_changed.emit(False)
        self.chat.messages_changed.emit()
        wait_js(self.panel, f"!document.querySelector('{RAIL}')")
        self.assertEqual(self.chat.sent, [])

    def test_nonempty_short_history_hides_navigation_without_scroll_overflow(self):
        self.publish_messages([{"id": f"short-{index}", "role": "user" if index < 2 else "assistant",
                                "content": f"Short owned message {index}"} for index in range(3)])
        result = eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport'), rail=document.querySelector(RAIL);
            return {maxScroll:viewport.scrollHeight-viewport.clientHeight,
                visibility:getComputedStyle(rail).visibility, hidden:rail.getAttribute('aria-hidden'), tabIndex:rail.tabIndex};
        })()""".replace("RAIL", json.dumps(RAIL)))
        self.assertLessEqual(result["maxScroll"], 1)
        self.assertEqual(result["visibility"], "hidden")
        self.assertEqual(result["hidden"], "true")
        self.assertEqual(result["tabIndex"], -1)
        self.assertEqual(self.chat.sent, [])

    def test_hidden_navigation_reappears_for_resize_and_content_growth(self):
        short = [{"id": f"grow-{index}", "role": "user" if index < 2 else "assistant",
                  "content": f"Short owned message {index}"} for index in range(3)]
        self.publish_messages(short)
        self.assertEqual(eval_js(self.panel, f"getComputedStyle(document.querySelector('{RAIL}')).visibility"), "hidden")
        eval_js(self.panel, f"(() => {{ window.qaHiddenRail=document.querySelector('{RAIL}'); return true; }})()")
        self.panel.resize(1000, 340)
        self.wait_for_renderer_size(self.panel)
        wait_js(self.panel, f"getComputedStyle(document.querySelector('{RAIL}')).visibility === 'visible'")
        self.assertGreater(eval_js(self.panel, "(() => {const v=document.querySelector('.thread-viewport'); return v.scrollHeight-v.clientHeight;})()"), 1)
        self.assertTrue(eval_js(self.panel, f"window.qaHiddenRail === document.querySelector('{RAIL}')"))
        self.panel.resize(1000, 720)
        self.wait_for_renderer_size(self.panel)
        wait_js(self.panel, f"getComputedStyle(document.querySelector('{RAIL}')).visibility === 'hidden'")
        grown = deepcopy(short)
        grown[2]["content"] = "Owned growing answer. " * 450
        self.publish_messages(grown)
        wait_js(self.panel, f"getComputedStyle(document.querySelector('{RAIL}')).visibility === 'visible'")
        self.assertTrue(eval_js(self.panel, f"window.qaHiddenRail === document.querySelector('{RAIL}')"))
        self.assertEqual(eval_js(self.panel, f"document.querySelector('{RAIL}').tabIndex"), 0)
        self.assertEqual(self.chat.sent, [])

    def test_content_shrink_hides_preview_preserves_draft_and_only_blurs_rail_focus(self):
        set_dom_text(self.panel, "#message-input", "Unsent owned rail draft")
        self.hover_index(12)
        eval_js(self.panel, "(() => {document.querySelector('#message-input').focus(); return true;})()")
        short = [{"id": f"shrink-{index}", "role": "user" if index < 2 else "assistant",
                  "content": f"Short owned message {index}"} for index in range(3)]
        self.publish_messages(short)
        self.assertEqual(eval_js(self.panel, f"getComputedStyle(document.querySelector('{RAIL}')).visibility"), "hidden")
        self.assertFalse(eval_js(self.panel, f"Boolean(document.querySelector('{PREVIEW}'))"))
        self.assertTrue(eval_js(self.panel, "document.activeElement === document.querySelector('#message-input')"))
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "Unsent owned rail draft")
        self.publish_messages(synthetic_messages())
        eval_js(self.panel, f"(() => {{document.querySelector('{RAIL}').focus(); return true;}})()")
        self.assertTrue(eval_js(self.panel, f"document.activeElement === document.querySelector('{RAIL}')"))
        self.publish_messages(short)
        self.assertFalse(eval_js(self.panel, f"document.activeElement === document.querySelector('{RAIL}')"))
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "Unsent owned rail draft")
        self.assertEqual(self.chat.sent, [])

    def test_hover_preview_is_literal_local_text_and_does_not_execute_markup(self):
        messages = deepcopy(self.chat.messages)
        literal = '<img src=x onerror="window.qaRailInjected=true"> Preview only #ffffff'
        messages[12]["content"] = literal
        self.publish_messages(messages)
        self.hover_index(12)
        result = eval_js(self.panel, """(() => {
            const preview=document.querySelector(PREVIEW), r=preview.getBoundingClientRect();
            return {text:preview.textContent, images:preview.querySelectorAll('img').length,
                executed:Boolean(window.qaRailInjected), fits:r.left>=0 && r.right<=innerWidth &&
                    r.top>=0 && r.bottom<=innerHeight};
        })()""".replace("PREVIEW", json.dumps(PREVIEW)))
        self.assertIn("Preview only", result["text"])
        self.assertEqual(result["images"], 0)
        self.assertFalse(result["executed"])
        self.assertTrue(result["fits"])
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.opened, [])

    def test_click_jump_keeps_draft_and_reading_position_when_layout_changes(self):
        before = deepcopy(self.chat.messages)
        set_dom_text(self.panel, "#message-input", "还没有发送的草稿")
        self.click_index(8)
        wait_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            return viewport.scrollHeight-viewport.clientHeight-viewport.scrollTop>300;
        })()""")
        self.panel.resize(800, 650)
        self.wait_for_renderer_size(self.panel)
        self.chat.reading_font_size = 20
        self.panel._queue_web_state()
        QTest.qWait(220)
        self.assertTrue(eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            return viewport.scrollHeight-viewport.clientHeight-viewport.scrollTop>300;
        })()"""), "Resizing after a jump must not snap back to the end")
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "还没有发送的草稿")
        self.assertEqual(self.chat.messages, before)
        self.assertEqual(self.chat.sent, [])
        self.assertFalse(any(call[0] in {"send", "open", "new", "clear"} for call in self.chat.calls))

    def test_wheel_over_ruler_scrolls_the_chat_without_moving_the_editor(self):
        set_dom_text(self.panel, "#message-input", "滚轮期间保留的草稿")
        self.click_web(self.panel, "#message-input")
        initial = eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            viewport.scrollTop=(viewport.scrollHeight-viewport.clientHeight)*.4;
            viewport.dispatchEvent(new Event('scroll'));
            return {top:viewport.scrollTop, page:document.scrollingElement.scrollTop};
        })()""")
        QTest.qWait(100)
        self.wheel_on_track(-120)
        wait_js(self.panel,
                "document.querySelector('.thread-viewport').scrollTop > "
                + json.dumps(initial["top"] + 1))
        after_down = eval_js(self.panel,
                             "document.querySelector('.thread-viewport').scrollTop")
        self.wheel_on_track(120)
        wait_js(self.panel,
                "document.querySelector('.thread-viewport').scrollTop < "
                + json.dumps(after_down - 1))
        self.assertEqual(eval_js(self.panel, "document.scrollingElement.scrollTop"), initial["page"])
        self.assertTrue(eval_js(self.panel,
            "document.activeElement === document.querySelector('#message-input')"))
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"),
                         "滚轮期间保留的草稿")
        self.assertEqual(self.chat.sent, [])

    def test_preview_card_click_jumps_to_its_message_and_preserves_the_draft(self):
        before = deepcopy(self.chat.messages)
        set_dom_text(self.panel, "#message-input", "预览卡片点击后也不能丢失")
        self.hover_index(12)
        button = ".conversation-rail-preview-jump"
        self.assertEqual(eval_js(self.panel,
            "document.querySelector(" + json.dumps(button) + ").tagName"), "BUTTON")
        self.assertEqual(eval_js(self.panel,
            "document.querySelector(" + json.dumps(button) + ").type"), "button")
        self.click_web(self.panel, button)
        self.wait_target_visible(12)
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"),
                         "预览卡片点击后也不能丢失")
        self.assertEqual(self.chat.messages, before)
        self.assertEqual(self.chat.sent, [])
        self.assertFalse(any(call[0] in {"send", "open", "new", "clear"} for call in self.chat.calls))

    def test_first_and_last_message_previews_stay_inside_the_window(self):
        for width in (1060, 420):
            self.panel.resize(width, 720)
            self.wait_for_renderer_size(self.panel)
            for index in (0, len(self.chat.messages) - 1):
                with self.subTest(width=width, message=index):
                    self.hover_index(index)
                    result = eval_js(self.panel, """(() => {
                        const preview=document.querySelector(PREVIEW), r=preview.getBoundingClientRect();
                        const title=preview.querySelector('.conversation-rail-preview-title');
                        const excerpt=preview.querySelector('.conversation-rail-preview-excerpt');
                        return {left:r.left, top:r.top, right:r.right, bottom:r.bottom,
                            width:innerWidth, height:innerHeight,
                            titleVisible:Boolean(title && title.getBoundingClientRect().height>0),
                            excerptVisible:Boolean(excerpt && excerpt.getBoundingClientRect().height>0),
                            overflow:document.documentElement.scrollWidth>innerWidth};
                    })()""".replace("PREVIEW", json.dumps(PREVIEW)))
                    self.assertGreaterEqual(result["left"], 7)
                    self.assertGreaterEqual(result["top"], 7)
                    self.assertLessEqual(result["right"], result["width"] - 7)
                    self.assertLessEqual(result["bottom"], result["height"] - 7)
                    self.assertTrue(result["titleVisible"] and result["excerptVisible"])
                    self.assertFalse(result["overflow"])

    def test_manual_scroll_updates_current_marker_without_taking_editor_focus(self):
        set_dom_text(self.panel, "#message-input", "a")
        eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            viewport.scrollTop=0; viewport.dispatchEvent(new Event('scroll')); return true;
        })()""")
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuenow') === '1'")
        eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            viewport.scrollTop=viewport.scrollHeight; viewport.dispatchEvent(new Event('scroll')); return true;
        })()""")
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuenow') === '48'")
        self.assertTrue(eval_js(self.panel, "document.activeElement === document.querySelector('#message-input')"))

    def test_dense_history_keeps_bounded_ticks_and_keyboard_reaches_every_message(self):
        self.publish_messages(synthetic_messages(400, "dense"))
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuemax') === '400'")
        count = eval_js(self.panel, "document.querySelectorAll('[data-testid=conversation-rail-tick]').length")
        self.assertGreater(count, 0)
        self.assertLessEqual(count, 72)
        self.click_web(self.panel, RAIL)
        target = self.keyboard_target(self.panel)
        QTest.keyClick(target, Qt.Key.Key_Home)
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuenow') === '1'")
        self.wait_target_visible(0)
        QTest.keyClick(target, Qt.Key.Key_Down)
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuenow') === '2'")
        QTest.keyClick(target, Qt.Key.Key_End)
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuenow') === '400'")
        self.wait_target_visible(399)
        QTest.keyClick(target, Qt.Key.Key_Escape)
        wait_js(self.panel, f"!document.querySelector('{PREVIEW}')")
        self.assertEqual(self.chat.sent, [])

    def test_preview_fits_light_dark_and_narrow_windows_and_closes_for_drawer(self):
        for theme in ("light", "dark"):
            self.theme = theme
            self.panel._queue_web_state()
            wait_js(self.panel, f"document.documentElement.dataset.theme === '{theme}'")
            for width in (1060, 420):
                with self.subTest(theme=theme, width=width):
                    self.panel.resize(width, 720)
                    self.wait_for_renderer_size(self.panel)
                    self.hover_index(24)
                    result = eval_js(self.panel, """(() => {
                        const r=document.querySelector(PREVIEW).getBoundingClientRect();
                        return {fits:r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight,
                            overflow:document.documentElement.scrollWidth>innerWidth};
                    })()""".replace("PREVIEW", json.dumps(PREVIEW)))
                    self.assertTrue(result["fits"])
                    self.assertFalse(result["overflow"])
            self.click_web(self.panel, "[data-testid=history-toggle]")
            wait_js(self.panel, f"!document.querySelector('{PREVIEW}')")
            self.assertFalse(eval_js(self.panel, f"Boolean(document.querySelector('{RAIL}'))"))
            wait_js(self.panel, "document.querySelector('.sidebar').getBoundingClientRect().left >= -1")
            QTest.qWait(30)
            self.click_web(self.panel, "[aria-label=收起历史会话]")
            wait_js(self.panel, f"Boolean(document.querySelector('{RAIL}'))")

    def test_typing_does_not_rebuild_the_ruler_or_change_reading_position(self):
        set_dom_text(self.panel, "#message-input", "a")
        self.click_web(self.panel, "#message-input")
        eval_js(self.panel, """(() => {
            const viewport=document.querySelector('.thread-viewport');
            viewport.scrollTop=(viewport.scrollHeight-viewport.clientHeight)*.35;
            viewport.dispatchEvent(new Event('scroll')); return true;
        })()""")
        QTest.qWait(120)
        started = Mock()
        self.panel.web_view.loadStarted.connect(started)
        eval_js(self.panel, """(() => {
            const rail=document.querySelector(RAIL), viewport=document.querySelector('.thread-viewport');
            const editor=document.querySelector('#message-input');
            window.qaRailTyping={rail,editor,top:viewport.scrollTop,railMutations:0,editorMutations:0};
            window.qaRailObserver=new MutationObserver(rows=>window.qaRailTyping.railMutations+=rows.length);
            window.qaEditorObserver=new MutationObserver(rows=>window.qaRailTyping.editorMutations+=rows.length);
            window.qaRailObserver.observe(rail,{subtree:true,childList:true,characterData:true});
            window.qaEditorObserver.observe(editor,{attributes:true,attributeFilter:['style']});
            return true;
        })()""".replace("RAIL", json.dumps(RAIL)))
        QTest.keyClicks(self.keyboard_target(self.panel), " steady navigation input", delay=3)
        wait_js(self.panel, "document.querySelector('#message-input').value === 'a steady navigation input'")
        QTest.qWait(100)
        result = eval_js(self.panel, """(() => {
            window.qaRailObserver.disconnect(); window.qaEditorObserver.disconnect();
            const previous=window.qaRailTyping;
            return {railMutations:previous.railMutations,editorMutations:previous.editorMutations,
                sameRail:previous.rail===document.querySelector(RAIL),
                sameEditor:previous.editor===document.querySelector('#message-input'),
                focused:document.activeElement===previous.editor,
                delta:Math.abs(previous.top-document.querySelector('.thread-viewport').scrollTop)};
        })()""".replace("RAIL", json.dumps(RAIL)))
        self.assertEqual(result["railMutations"], 0)
        self.assertEqual(result["editorMutations"], 0)
        self.assertTrue(result["sameRail"] and result["sameEditor"] and result["focused"])
        self.assertLessEqual(result["delta"], 1)
        self.assertEqual(self.chat.sent, [])
        started.assert_not_called()

    def test_new_conversation_discards_the_old_preview_and_marker_targets(self):
        self.hover_index(12)
        self.chat.new_conversation()
        wait_js(self.panel, f"!document.querySelector('{RAIL}') && !document.querySelector('{PREVIEW}')")
        self.chat.messages = synthetic_messages(8, "new")
        self.chat.messages_changed.emit()
        wait_js(self.panel, f"document.querySelector('{RAIL}')?.getAttribute('aria-valuemax') === '8'")
        self.assertEqual(eval_js(self.panel,
            "document.querySelectorAll('[data-rail-message-id^=rail-]').length"), 0)
        self.hover_index(2)
        self.assertEqual(eval_js(self.panel, f"document.querySelector('{PREVIEW}').dataset.previewMessageId"), "new-002")
        self.assertEqual(self.chat.sent, [])


if __name__ == "__main__":
    unittest.main()
