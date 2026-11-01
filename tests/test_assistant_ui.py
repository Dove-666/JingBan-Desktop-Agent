"""Exercise the shipped React chat through Qt WebEngine and its real bridge.

These tests use synthetic messages and in-memory controller doubles.  They never
open the user's conversation archive, credential store, or a model connection.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from tests.test_session_ui import SessionChat


REPOSITORY = Path(__file__).resolve().parents[1]


def wait_until(condition, timeout: float = 10.0, message: str = "WebEngine condition timed out"):
    """Pump Qt while waiting for renderer-process callbacks on Windows."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        QTest.qWait(10)
        time.sleep(0.002)
    if not condition():
        raise AssertionError(message)


def eval_js(panel, expression: str, timeout: float = 6.0):
    """Return JSON from the visible page, rather than inspect hidden Qt widgets."""
    result = []
    panel.web_view.page().runJavaScript(
        "(() => { try { return JSON.stringify({value:(" + expression +
        ")}); } catch(error) { return JSON.stringify({error:String(error)}); } })()",
        result.append,
    )
    wait_until(lambda: bool(result), timeout, "The WebEngine JavaScript callback did not arrive")
    payload = json.loads(result[0] or "{}")
    if "error" in payload:
        raise AssertionError(payload["error"])
    return payload.get("value")


def wait_js(panel, expression: str, timeout: float = 10.0):
    wait_until(lambda: bool(eval_js(panel, expression)), timeout,
               "The rendered chat did not reach: " + expression)


def set_dom_text(panel, selector: str, value: str):
    """Send the input event React expects after changing a native text value."""
    expression = """(() => {
      const element = document.querySelector(SELECTOR);
      if (!element) throw new Error('Missing input: ' + SELECTOR);
      const prototype = element.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, VALUE);
      element.dispatchEvent(new Event('input', {bubbles:true}));
      element.dispatchEvent(new Event('change', {bubbles:true}));
      element.focus();
      return true;
    })()""".replace("SELECTOR", json.dumps(selector)).replace("VALUE", json.dumps(value))
    eval_js(panel, expression)
    QTest.qWait(40)


def click_dom(panel, selector: str):
    return eval_js(panel, "(() => {const target=document.querySelector(" + json.dumps(selector) +
                   "); if(!target) throw new Error('Missing DOM target'); target.click(); return true;})()")


def bridge_action(panel, action: str, **payload):
    return json.loads(panel.bridge.dispatch(action, json.dumps(payload)))


def open_row_menu(panel, conversation_id):
    selector = "[data-conversation-id=" + json.dumps(conversation_id) + "] .row-actions button"
    click_dom(panel, selector)
    wait_js(panel, "Boolean(document.querySelector('.row-popover'))")


def click_row_action(panel, conversation_id, keyword):
    open_row_menu(panel, conversation_id)
    eval_js(panel, """(() => {
        const button=Array.from(document.querySelectorAll('.row-popover button'))
            .find(node=>node.textContent.includes(KEYWORD));
        if (!button) throw new Error('Missing conversation action');
        if (button.disabled) throw new Error('Conversation action is disabled');
        button.click(); return true;
    })()""".replace("KEYWORD", json.dumps(keyword)))


def layout_metrics(panel):
    return eval_js(panel, """(() => {
      const composer=document.querySelector('.composer').getBoundingClientRect();
      const main=document.querySelector('.main-panel').getBoundingClientRect();
      const sidebar=document.querySelector('.sidebar').getBoundingClientRect();
      return {width:innerWidth,height:innerHeight,
        documentWidth:document.documentElement.scrollWidth,
        bodyWidth:document.body.scrollWidth,
        composer:{left:composer.left,right:composer.right,top:composer.top,bottom:composer.bottom},
        main:{left:main.left,right:main.right},
        sidebar:{left:sidebar.left,right:sidebar.right},
        codeBlocks:document.querySelectorAll('pre').length};
    })()""")


class AssistantUiChat(SessionChat):
    """Deterministic controller surface including export and request progress."""

    export_busy_changed = Signal(bool)
    export_finished = Signal(str, str, str)
    request_status_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.error = ""
        self.error_action = ""
        self.error_conversation_id = ""
        self.error_message_id = ""
        self.request_conversation_id = self.current_conversation_id
        self.can_stop = False
        self.request_status = ""
        self.export_busy = False
        self.accept_sends = True
        self.sent = []
        self.messages = [
            {"id": "qa-user-1", "role": "user", "source": "user", "content": "怎么整理桌面上的任务？"},
            {"id": "qa-assistant-1", "role": "assistant", "source": "assistant",
             "content": "可以先选一件现在最想完成的事，我们一起把它拆成几步。"},
        ]

    def send(self, text):
        self.calls.append(("send", text))
        if self.busy or self.history_loading or not self.accept_sends:
            return False
        self.sent.append(text)
        self.messages.append({"id": f"qa-sent-{len(self.sent)}", "role": "user", "source": "user", "content": text})
        self.messages_changed.emit()
        return True

    def export_conversation(self, conversation_id, destination, format="markdown"):
        self.calls.append(("export", conversation_id, destination, format))
        self.export_busy = True
        self.export_busy_changed.emit(True)
        return True

    def clear(self):
        self.calls.append(("clear", self.current_conversation_id))
        super().clear()

    def retry(self, conversation_id=None, message_id=None):
        self.calls.append(("retry", conversation_id, message_id))
        return (not self.busy and not self.history_loading
                and conversation_id == self.current_conversation_id and bool(message_id))

    def stop_request(self, conversation_id):
        self.calls.append(("stop", conversation_id))
        if not self.can_stop or conversation_id != self.request_conversation_id:
            return False
        self.can_stop = False
        self.busy = False
        self.busy_changed.emit(False)
        return True

    def reload_history(self, conversation_id):
        self.calls.append(("reloadHistory", conversation_id))
        return bool(conversation_id)


class DeferredHistoryChat(AssistantUiChat):
    """Match real history placeholders; finish/fail only when a test releases it."""

    def __init__(self):
        super().__init__()
        self.pending_target = None
        self._restore_view = None
        self.histories = {
            "current": deepcopy(self.messages),
            "old": [{"id": "old-source", "role": "assistant", "content": "只属于旧会话的内容"}],
        }

    def open_conversation(self, conversation_id, focus_message_id=None):
        self.calls.append(("open", conversation_id))
        if conversation_id not in self.histories:
            return False
        if not self.history_loading:
            self._restore_view = (self.current_conversation_id, self.current_title, deepcopy(self.messages))
        self.pending_target = conversation_id
        self.current_conversation_id = conversation_id
        self.current_title = "正在加载会话…"
        self.messages = []
        self.history_loading = True
        self.error = ""
        self.history_busy_changed.emit(True)
        self.error_changed.emit("")
        self.conversation_changed.emit()
        self.messages_changed.emit()
        return True

    def finish_open(self, error=""):
        if self.pending_target is None:
            return
        target, self.pending_target = self.pending_target, None
        self.history_loading = False
        if error:
            self.current_conversation_id, self.current_title, self.messages = self._restore_view
            self.error = "历史会话加载失败：" + error
            self.error_action = "history"
            self.error_conversation_id = target
        else:
            self.current_title = next(item["title"] for item in self.conversations if item["id"] == target)
            self.messages = deepcopy(self.histories[target])
            self.error = ""
            self.error_action = ""
        self._restore_view = None
        self.history_busy_changed.emit(False)
        self.error_changed.emit(self.error)
        self.conversation_changed.emit()
        self.messages_changed.emit()

    def new_conversation(self):
        self.pending_target = None
        self._restore_view = None
        self.history_loading = False
        self.history_busy_changed.emit(False)
        return super().new_conversation()

    def clear(self):
        self.histories[self.current_conversation_id] = []
        super().clear()


class AssistantUiPerception(QObject):
    """Memory-only perception state; no screen reader, capture, or network."""

    runtime_changed = Signal()
    settings_requested = Signal()

    def __init__(self):
        super().__init__()
        self.paused = False
        self.calls = []

    def runtime_snapshot(self):
        return {"enabled": True, "allowRemote": False, "paused": self.paused,
                "busy": False, "status": "合成状态：仅本地观察", "cooldownSeconds": 42,
                "hourlyRemaining": 3, "nextObservationSeconds": 5,
                "nextAnalysisSeconds": 12, "previewAgeSeconds": None}

    def set_paused(self, paused):
        self.paused = paused
        self.calls.append(("pause", paused))
        self.runtime_changed.emit()


def make_panel(chat=None, **kwargs):
    # Import lazily so ordinary helper imports do not create a WebEngine page.
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    panel = AssistantChatPanel(chat or AssistantUiChat(), **kwargs)
    panel.resize(1000, 720)
    panel.show()
    wait_until(lambda: panel.web_view is not None, message="The Qt panel did not create its WebEngine view")
    wait_js(panel, "Boolean(document.querySelector('textarea'))")
    wait_until(lambda: panel._frontend_ready, message="The local React frontend did not finish the WebChannel handshake")
    return panel


class AssistantUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = AssistantUiChat()
        self.opened = []
        self.panel = make_panel(
            self.chat,
            memory_opener=lambda: self.opened.append("memory"),
            settings_opener=lambda: self.opened.append("settings"),
        )
        self.extra_panels = []

    def tearDown(self):
        for panel in [self.panel, *self.extra_panels]:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_local_bundle_mounts_real_messages_and_composer(self):
        wait_js(self.panel, "document.querySelector('.header-title')?.textContent === '开发桌宠'")
        self.assertIn("桌面上的任务", eval_js(self.panel, "document.body.innerText"))
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('[data-message-id]').length"), 2)
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').tagName"), "TEXTAREA")
        self.assertTrue(eval_js(self.panel, "document.querySelector('.send-button').disabled"))
        self.assertEqual(eval_js(self.panel,
                                 "document.querySelectorAll('.local-note, .keyboard-hint, .composer-footnote, "
                                 ".perception-status, .camera-runtime').length"), 0)
        self.assertEqual(eval_js(self.panel,
                                 "document.querySelectorAll('[data-testid=tools-button], .header-actions button, "
                                 ".chat-header button[aria-label^=新对话]').length"), 0)
        self.assertTrue(self.panel.web_view.url().isLocalFile())

    def test_actual_dom_send_reaches_controller_and_second_view(self):
        other = make_panel(self.chat)
        self.extra_panels.append(other)
        set_dom_text(self.panel, "#message-input", "从实际 React 输入框发送")
        wait_js(self.panel, "!document.querySelector('.send-button').disabled")
        click_dom(self.panel, ".send-button")
        wait_until(lambda: self.chat.sent == ["从实际 React 输入框发送"])
        for panel in (self.panel, other):
            wait_js(panel, "document.body.innerText.includes('从实际 React 输入框发送')")
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")

    def test_stale_send_is_rejected_and_draft_remains(self):
        set_dom_text(self.panel, "#message-input", "不能被过期命令清空的草稿")
        result = bridge_action(self.panel, "send", text="发到错误会话", conversationId="old")
        self.assertFalse(result["ok"])
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"),
                         "不能被过期命令清空的草稿")

    def test_rejected_send_keeps_editor_and_can_be_retried(self):
        self.chat.accept_sends = False
        set_dom_text(self.panel, "#message-input", "发送失败时保留的草稿")
        click_dom(self.panel, ".send-button")
        wait_until(lambda: ("send", "发送失败时保留的草稿") in self.chat.calls)
        wait_js(self.panel, "document.querySelector('#message-input').value === '发送失败时保留的草稿'")
        self.chat.accept_sends = True
        wait_js(self.panel, "!document.querySelector('.send-button').disabled")
        click_dom(self.panel, ".send-button")
        wait_until(lambda: self.chat.sent == ["发送失败时保留的草稿"])

    def test_chinese_composition_and_shift_enter_do_not_send(self):
        set_dom_text(self.panel, "#message-input", "中文输入法正在选字")
        eval_js(self.panel, """(() => {
          const input=document.querySelector('#message-input');
          input.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'ni'}));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:229,isComposing:true,bubbles:true}));
          input.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'你'}));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',shiftKey:true,bubbles:true}));
          return true;
        })()""")
        QTest.qWait(120)
        self.assertFalse(self.chat.sent)
        eval_js(self.panel, "document.querySelector('#message-input').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))")
        wait_until(lambda: self.chat.sent == ["中文输入法正在选字"])

    def test_session_switch_preserves_independent_react_drafts(self):
        set_dom_text(self.panel, "#message-input", "当前会话草稿")
        self.chat.open_conversation("old")
        wait_js(self.panel, "document.querySelector('.header-title')?.textContent === '旧会话'")
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "")
        set_dom_text(self.panel, "#message-input", "旧会话自己的草稿")
        self.chat.open_conversation("current")
        wait_js(self.panel, "document.querySelector('#message-input').value === '当前会话草稿'")
        self.chat.open_conversation("old")
        wait_js(self.panel, "document.querySelector('#message-input').value === '旧会话自己的草稿'")
        self.panel._reload_web()
        wait_until(lambda: self.panel._frontend_ready, message="The reloaded WebChannel did not reconnect")
        wait_js(self.panel, "document.querySelector('#message-input').value === '旧会话自己的草稿'")
        self.chat.open_conversation("current")
        wait_js(self.panel, "document.querySelector('#message-input').value === '当前会话草稿'")

    def test_actual_history_search_is_debounced(self):
        # The initial history refresh finishes before the frontend is ready.
        # Only edits made by the user belong to this debounce assertion.
        before = len(self.chat.calls)
        click_dom(self.panel, "button[aria-label='搜索会话 · Ctrl+F']")
        wait_js(self.panel, "Boolean(document.querySelector('.search-field input'))")
        set_dom_text(self.panel, ".search-field input", "桌")
        set_dom_text(self.panel, ".search-field input", "桌宠 API")
        wait_until(lambda: any(call == ("search", "桌宠 API") for call in self.chat.calls))
        self.assertEqual([call for call in self.chat.calls[before:] if call[0] == "search"], [("search", "桌宠 API")])

    def test_long_markdown_stays_within_window_at_three_widths(self):
        self.chat.messages = [
            {"id": "qa-long-user", "role": "user", "content": "请帮我检查这个很长的地址：" + "https://example.invalid/" + "abcdef" * 120},
            {"id": "qa-long-assistant", "role": "assistant", "content": "这是代码示例：\n\n```python\nresult = '" + "long_value_" * 100 + "'\n```\n\n" + "连续中文文本" * 100},
        ]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('pre').length === 1")
        for width in (640, 900, 1200):
            with self.subTest(width=width):
                self.panel.resize(width, 720)
                QTest.qWait(120)
                metrics = layout_metrics(self.panel)
                self.assertLessEqual(metrics["documentWidth"], metrics["width"] + 1, metrics)
                self.assertLessEqual(metrics["bodyWidth"], metrics["width"] + 1, metrics)
                self.assertGreaterEqual(metrics["composer"]["left"], -1, metrics)
                self.assertLessEqual(metrics["composer"]["right"], metrics["width"] + 1, metrics)
                self.assertLessEqual(metrics["composer"]["bottom"], metrics["height"] + 1, metrics)

    def test_provider_html_and_remote_images_are_inert(self):
        self.chat.messages = [{"id": "qa-untrusted", "role": "assistant", "content":
                              '<img src="https://invalid.test/remote.png" onerror="window.qaInjection=1">'
                              '<script>window.qaInjection=2</script>\n\n![远程图片](https://invalid.test/markdown.png)\n\n'
                              '[外部链接](https://invalid.test/navigation)'}]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "Boolean(document.querySelector('[data-message-id=qa-untrusted]'))")
        self.assertFalse(eval_js(self.panel, "Boolean(window.qaInjection)"))
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.markdown img,.markdown iframe,.markdown script').length"), 0)
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.markdown a[href]').length"), 1)
        self.assertEqual(eval_js(self.panel, "document.querySelector('.markdown a').getAttribute('href')"),
                         "https://invalid.test/navigation")

    def test_user_request_failures_show_per_message_retry_with_explicit_ids(self):
        statuses = {"failed": "回复失败", "cancelled": "已停止生成", "interrupted": "上次请求未完成",
                    "pending": "正在等待回复"}
        self.chat.messages = [{"id": "state-" + status, "role": "user", "content": "合成请求：" + status,
                               "metadata": {"request_status": status}} for status in statuses]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('[data-testid=message-request-state]').length === 4")
        for status, label in statuses.items():
            with self.subTest(status=status):
                selector = f"[data-message-id=state-{status}] .request-message-state"
                self.assertIn(label, eval_js(self.panel, f"document.querySelector({json.dumps(selector)}).innerText"))
                self.assertEqual(eval_js(self.panel, f"document.querySelectorAll({json.dumps(selector + ' button')}).length"),
                                 0 if status == "pending" else 1)
                if status != "pending":
                    click_dom(self.panel, selector + " button")
                    wait_until(lambda: ("retry", "current", "state-" + status) in self.chat.calls)
        self.assertFalse(bridge_action(self.panel, "retry", conversationId="old", messageId="state-failed")["ok"])
        self.assertFalse(bridge_action(self.panel, "retry", conversationId="current")["ok"])
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        wait_js(self.panel, "[...document.querySelectorAll('.request-message-state button')].every(b=>b.disabled)")
        self.assertEqual([call for call in self.chat.calls if call[0] == "retry"],
                         [("retry", "current", "state-" + status) for status in ("failed", "cancelled", "interrupted")])

    def test_error_banner_routes_settings_history_and_reply_recovery(self):
        routes = (("settings", "打开 API 设置"), ("history", "重新读取"), ("retry", "重试回复"))
        for action, label in routes:
            with self.subTest(action=action):
                self.chat.error = "合成错误：" + action
                self.chat.error_action = action
                self.chat.error_conversation_id = "current" if action == "retry" else "old"
                self.chat.error_message_id = "qa-user-1"
                self.chat.error_changed.emit(self.chat.error)
                wait_js(self.panel, "document.querySelector('.error-banner button')?.textContent === " + json.dumps(label))
                before = len(self.chat.calls)
                click_dom(self.panel, ".error-banner button")
                if action == "settings":
                    wait_until(lambda: self.opened == ["settings"])
                    self.assertEqual(len(self.chat.calls), before)
                else:
                    expected = ("reloadHistory", "old") if action == "history" else ("retry", "current", "qa-user-1")
                    wait_until(lambda: expected in self.chat.calls)
                    self.assertEqual(self.chat.calls[before:], [expected])
        self.chat.error_action = ""
        self.chat.error_changed.emit(self.chat.error)
        wait_js(self.panel, "!document.querySelector('.error-banner button')")

    def test_stop_button_targets_request_owner_after_switching_conversation(self):
        self.chat.busy = True
        self.chat.can_stop = True
        self.chat.request_conversation_id = "current"
        self.chat._active_request_conversation = "current"
        self.chat.open_conversation("old")
        self.chat.busy_changed.emit(True)
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=stop-button]'))")
        self.assertTrue(eval_js(self.panel, "document.querySelector('.send-button').disabled"))
        click_dom(self.panel, "[data-testid=stop-button]")
        wait_until(lambda: ("stop", "current") in self.chat.calls)
        wait_js(self.panel, "!document.querySelector('[data-testid=stop-button]')")
        self.assertEqual(self.chat.current_conversation_id, "old")

    def test_safe_external_links_open_and_copy_through_native_host(self):
        urls = ["https://example.test/help?q=hello#part", "http://example.test/manual"]
        self.chat.messages = [{"id": "qa-links", "role": "assistant", "content":
                               "[安全 HTTPS](" + urls[0] + ") [安全 HTTP](" + urls[1] + ")\n\n"
                               "[脚本](javascript:alert%281%29) [本机](file:///C:/fake.txt) "
                               "[凭据](https://fake:secret@example.test/private) "
                               "[数据](data:text/html,hello)"}]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('.message-link[href]').length === 2")
        self.assertEqual(eval_js(self.panel, "[...document.querySelectorAll('.message-link')].map(a=>a.href)"), urls)
        clipboard = Mock()
        with patch("desktop_pet.ui.assistant_panel.QDesktopServices.openUrl", return_value=True) as opener, \
                patch("desktop_pet.ui.assistant_panel.QApplication.clipboard", return_value=clipboard):
            click_dom(self.panel, ".message-link")
            wait_until(lambda: opener.call_count == 1)
            self.assertEqual(opener.call_args.args[0].toString(), urls[0])
            click_dom(self.panel, ".link-copy")
            wait_until(lambda: clipboard.setText.call_count == 1)
            self.assertEqual(clipboard.setText.call_args.args[0], urls[0])
            wait_js(self.panel, "document.querySelector('.link-copy').title === '已复制链接'")
            self.assertTrue(self.panel.web_view.url().isLocalFile())
            for invalid in ("javascript:alert(1)", "file:///C:/fake.txt", "data:text/html,hello",
                            "https://fake:secret@example.test/private", "https://example.test/\ninvalid",
                            "//example.test/path", "https:///missing-host"):
                with self.subTest(invalid=invalid):
                    self.assertFalse(bridge_action(self.panel, "openExternal", url=invalid)["ok"])
                    self.assertFalse(bridge_action(self.panel, "copyLink", url=invalid)["ok"])
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(clipboard.setText.call_count, 1)

    def test_history_rows_show_only_titles_even_when_dates_and_summaries_exist(self):
        self.chat.conversations = [
            {"id": "current", "title": "合成标题 <script>文本</script>", "updated_at": "2026-10-31T10:30:00",
             "summary": "摘要保留 <script>文本</script> 与 " + "很长的摘要" * 30, "message_count": 8},
            {"id": "old", "title": "无摘要对话", "updated_at": "", "summary": "", "message_count": 0},
        ]
        self.chat.conversations_changed.emit()
        wait_js(self.panel, "document.querySelector('.conversation-title').textContent.includes('合成标题')")
        self.assertEqual(eval_js(self.panel, "document.querySelector('.conversation-link').textContent"),
                         self.chat.conversations[0]["title"])
        self.assertEqual(eval_js(self.panel, "document.querySelector('.conversation-link').title"),
                         self.chat.conversations[0]["title"])
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.conversation-time, .conversation-summary, .conversation-list time').length"), 0)
        self.assertNotIn("摘要保留", eval_js(self.panel, "document.querySelector('.conversation-list').textContent"))
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.conversation-list script').length"), 0)
        self.assertEqual(self.panel.web_state()["conversations"][0]["messageCount"], 8)
        self.assertLessEqual(layout_metrics(self.panel)["documentWidth"], layout_metrics(self.panel)["width"] + 1)

    def test_420px_drawer_traps_real_qt_tab_and_escape_returns_to_trigger(self):
        self.panel.resize(420, 720)
        wait_js(self.panel, "innerWidth === 420 && document.querySelector('.sidebar').getBoundingClientRect().right <= 1")
        self.panel.web_view.setFocus()
        eval_js(self.panel, "document.querySelector('[data-testid=history-toggle]').focus()")
        keyboard_target = self.panel.web_view.focusProxy() or self.panel.web_view
        QTest.keyClick(keyboard_target, Qt.Key.Key_Return)
        wait_js(self.panel, "document.querySelector('.sidebar').getAttribute('aria-modal') === 'true'")
        wait_js(self.panel, "document.querySelector('.sidebar').contains(document.activeElement)")
        self.assertTrue(eval_js(self.panel, "document.querySelector('.main-panel').inert"))
        eval_js(self.panel, "document.querySelector('.sidebar button').focus()")
        QTest.keyClick(keyboard_target, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        wait_js(self.panel, "document.activeElement === document.querySelector('.sidebar-setting')")
        QTest.keyClick(keyboard_target, Qt.Key.Key_Tab)
        wait_js(self.panel, "document.activeElement === document.querySelector('.sidebar button')")
        for _ in range(16):
            QTest.keyClick(keyboard_target, Qt.Key.Key_Tab)
            QTest.qWait(20)
            self.assertTrue(eval_js(self.panel, "document.querySelector('.sidebar').contains(document.activeElement)"))
        QTest.keyClick(keyboard_target, Qt.Key.Key_Escape)
        wait_js(self.panel, "document.querySelector('.sidebar').getBoundingClientRect().right <= 1")
        wait_js(self.panel, "document.activeElement === document.querySelector('[data-testid=history-toggle]')")
        self.assertFalse(eval_js(self.panel, "document.querySelector('.main-panel').inert"))

    def test_webengine_failure_compatibility_mode_shares_drafts_and_returns_to_white_ui(self):
        set_dom_text(self.panel, "#message-input", "新版当前会话草稿")
        wait_until(lambda: self.panel._web_drafts.get("current") == "新版当前会话草稿")
        self.panel._web_failed()
        self.assertTrue(self.panel.compatibility_button.isVisible())
        QTest.mouseClick(self.panel.compatibility_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.panel._native_mode)
        self.assertTrue(self.panel.splitter.isVisible())
        self.assertEqual(self.panel.input.toPlainText(), "新版当前会话草稿")
        self.panel.input.setPlainText("兼容界面修改的当前草稿")
        self.chat.open_conversation("old")
        self.assertEqual(self.panel.input.toPlainText(), "")
        self.panel.input.setPlainText("兼容界面的旧会话草稿")
        self.chat.open_conversation("current")
        self.assertEqual(self.panel.input.toPlainText(), "兼容界面修改的当前草稿")
        QTest.mouseClick(self.panel.reload_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.panel._frontend_ready)
        wait_js(self.panel, "document.querySelector('#message-input')?.value === '兼容界面修改的当前草稿'")
        self.assertFalse(self.panel._native_mode)
        self.assertTrue(self.panel.web_view.isVisible())
        self.assertFalse(self.panel.splitter.isVisible())
        self.assertEqual(eval_js(self.panel, "getComputedStyle(document.documentElement).colorScheme"), "light")
        self.chat.open_conversation("old")
        wait_js(self.panel, "document.querySelector('#message-input').value === '兼容界面的旧会话草稿'")

    def test_missing_webengine_enters_compatibility_mode_and_can_retry_loading(self):
        from desktop_pet.ui.assistant_panel import AssistantChatPanel
        chat = AssistantUiChat()
        other = AssistantChatPanel(chat)
        self.extra_panels.append(other)
        other.resize(900, 720)
        with patch.object(other, "_create_web", side_effect=ImportError("synthetic WebEngine unavailable")):
            other.show()
            wait_until(lambda: other.compatibility_button.isVisible())
            self.assertIsNone(other.web_view)
            QTest.mouseClick(other.compatibility_button, Qt.MouseButton.LeftButton)
            self.assertTrue(other._native_mode)
            other.input.setPlainText("缺少渲染器时仍可发送")
            QTest.mouseClick(other.send_button, Qt.MouseButton.LeftButton)
            self.assertEqual(chat.sent, ["缺少渲染器时仍可发送"])
            other.input.setPlainText("恢复渲染器后保留的草稿")
        QTest.mouseClick(other.reload_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: other._frontend_ready)
        wait_js(other, "document.querySelector('#message-input')?.value === '恢复渲染器后保留的草稿'")
        self.assertFalse(other._native_mode)

    def test_enabled_perception_does_not_add_settings_or_pause_controls_to_row_menu(self):
        self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('.perception-status'))"))
        perception = AssistantUiPerception()
        self.chat.perception_controller = perception
        other = make_panel(self.chat)
        self.extra_panels.append(other)
        self.assertEqual(eval_js(other,
                                 "document.querySelectorAll('.perception-status, .camera-runtime').length"), 0)
        open_row_menu(other, "current")
        self.assertEqual(eval_js(other, "document.querySelectorAll('.row-popover button').length"), 5)
        self.assertEqual(eval_js(other, "document.querySelectorAll('[data-testid=screen-pause-button], "
                                 "[data-testid=camera-pause-button], [data-testid=visual-pause-button]').length"), 0)
        self.assertFalse(eval_js(other, "document.querySelector('.row-popover').innerText.includes('屏幕感知')"))
        self.assertEqual(perception.calls, [])
        self.assertFalse(perception.paused)

    def test_screen_message_focus_reveals_context(self):
        self.chat.messages.append({"id": "qa-screen", "role": "assistant", "source": "proactive_screen",
                                   "content": "编辑器里的异常可以继续一起排查。",
                                   "screen_event": {"app_name": "Code.exe", "event_type": "coding_error",
                                                    "observed_at": "2026-10-31 10:30", "context_summary": "QA_EVENT_CONTEXT：缺少依赖包。"}})
        self.chat.messages_changed.emit()
        wait_js(self.panel, "Boolean(document.querySelector('[data-message-id=qa-screen]'))")
        self.chat.message_focus_requested.emit("qa-screen")
        wait_js(self.panel, "document.querySelector('[data-message-id=qa-screen] .event-details')?.innerText.includes('QA_EVENT_CONTEXT')")
        click_dom(self.panel, "[data-message-id=qa-screen] .event-toggle")
        wait_js(self.panel, "!document.querySelector('[data-message-id=qa-screen] .event-details')")
        self.chat.context_usage = "用于验证普通状态刷新不会重新展开屏幕情境。"
        self.chat.context_usage_changed.emit(self.chat.context_usage)
        self.chat.messages.append({"id": "qa-after-screen", "role": "assistant", "content": "普通新回复。"})
        self.chat.messages_changed.emit()
        wait_js(self.panel, "Boolean(document.querySelector('[data-message-id=qa-after-screen]'))")
        self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('[data-message-id=qa-screen] .event-details'))"))
        self.assertTrue(eval_js(self.panel, "document.querySelector('[data-message-id=qa-screen]').classList.contains('message-highlight')"),
                        "An ordinary host snapshot must not prematurely remove the focus indication")

    def test_row_menu_contains_five_conversation_actions_and_settings_remain_in_sidebar(self):
        open_row_menu(self.panel, "current")
        labels = eval_js(self.panel, """Array.from(document.querySelectorAll('.row-popover button')).map(node => {
            const copy=node.cloneNode(true); copy.querySelectorAll('kbd').forEach(key=>key.remove());
            return copy.textContent.trim();
        })""")
        self.assertEqual(len(labels), 5)
        for keyword in ("查找", "导出", "清空", "重命名", "删除"):
            self.assertEqual(sum(keyword in label for label in labels), 1, labels)
        self.assertEqual(eval_js(self.panel,
                                 "document.querySelectorAll('.row-popover .reading-controls, "
                                 ".row-popover .context-note, .row-popover .observation-notice').length"), 0)
        self.assertNotIn(self.chat.context_usage, eval_js(self.panel, "document.querySelector('.row-popover').innerText"))
        eval_js(self.panel, "window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        wait_js(self.panel, "!document.querySelector('.row-popover')")
        click_dom(self.panel, ".sidebar-setting")
        wait_until(lambda: self.opened == ["settings"])
        self.assertEqual(self.chat.current_conversation_id, "current")

    def test_sidebar_and_ctrl_n_create_conversations_without_duplicate_header_button(self):
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.header-actions > button').length"), 0)
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.chat-header button[aria-label^=新对话]').length"), 0)
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.sidebar .new-conversation').length"), 1)
        set_dom_text(self.panel, "#message-input", "切换会话前保留的草稿")
        wait_until(lambda: self.panel._web_drafts.get("current") == "切换会话前保留的草稿")
        click_dom(self.panel, ".sidebar .new-conversation")
        wait_until(lambda: self.chat.calls.count(("new",)) == 1)
        wait_js(self.panel, "document.querySelector('.header-title')?.textContent === '新对话'")
        self.panel.web_view.setFocus()
        eval_js(self.panel, "document.querySelector('#message-input').focus()")
        keyboard_target = self.panel.web_view.focusProxy() or self.panel.web_view
        QTest.keyClick(keyboard_target, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: self.chat.calls.count(("new",)) == 2)
        self.assertEqual(self.chat.current_conversation_id, "new-session")
        self.assertEqual(self.panel._web_drafts["current"], "切换会话前保留的草稿")
        self.assertEqual(self.chat.sent, [])

    def test_row_find_opens_its_conversation_and_keeps_the_previous_draft(self):
        set_dom_text(self.panel, "#message-input", "当前会话尚未发出的草稿")
        wait_until(lambda: self.panel._web_drafts.get("current") == "当前会话尚未发出的草稿")
        click_row_action(self.panel, "old", "查找")
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=message-search]'))")
        self.assertEqual(self.chat.current_conversation_id, "old")
        self.assertIn(("open", "old"), self.chat.calls)
        set_dom_text(self.panel, "[data-testid=message-search]", "历史记录")
        wait_js(self.panel, "document.querySelector('.message-find-count').textContent.includes('1 / 1')")
        self.chat.open_conversation("current")
        wait_js(self.panel, "document.querySelector('#message-input').value === '当前会话尚未发出的草稿'")
        self.panel.web_view.setFocus()
        eval_js(self.panel, "document.querySelector('#message-input').focus()")
        QTest.keyClick(self.panel.web_view.focusProxy() or self.panel.web_view, Qt.Key.Key_F,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=message-search]'))")
        self.assertEqual(self.chat.current_conversation_id, "current")

    def test_row_export_uses_the_selected_old_conversation_instead_of_the_current_one(self):
        set_dom_text(self.panel, "#message-input", "导出旧会话也不能丢失这段草稿")
        wait_until(lambda: self.panel._web_drafts.get("current") == "导出旧会话也不能丢失这段草稿")
        with patch.object(QFileDialog, "getSaveFileName", return_value=("qa-row-export-not-written", "JSON 对话 (*.json)")):
            click_row_action(self.panel, "old", "导出")
            wait_until(lambda: any(call[0] == "export" for call in self.chat.calls))
        exports = [call for call in self.chat.calls if call[0] == "export"]
        self.assertEqual(exports, [("export", "old", "qa-row-export-not-written.json", "json")])
        self.assertEqual(self.chat.current_conversation_id, "old")
        self.assertEqual(self.panel._web_drafts["current"], "导出旧会话也不能丢失这段草稿")

    def test_row_clear_waits_for_selected_history_then_only_clears_that_history(self):
        chat = DeferredHistoryChat()
        other = make_panel(chat)
        self.extra_panels.append(other)
        current_history = deepcopy(chat.histories["current"])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as confirm:
            click_row_action(other, "old", "清空")
            wait_until(lambda: chat.history_loading)
            confirm.assert_not_called()
            self.assertNotIn(("clear", "current"), chat.calls)
            chat.finish_open()
            wait_until(lambda: ("clear", "old") in chat.calls)
        self.assertEqual(chat.histories["old"], [])
        self.assertEqual(chat.histories["current"], current_history)
        self.assertEqual([call for call in chat.calls if call[0] == "clear"], [("clear", "old")])

    def test_row_export_waits_until_history_has_loaded_before_showing_native_picker(self):
        chat = DeferredHistoryChat()
        other = make_panel(chat)
        self.extra_panels.append(other)
        with patch.object(QFileDialog, "getSaveFileName", return_value=("qa-delayed-not-written", "JSON 对话 (*.json)")) as picker:
            click_row_action(other, "old", "导出")
            wait_until(lambda: chat.history_loading)
            picker.assert_not_called()
            self.assertFalse(any(call[0] == "export" for call in chat.calls))
            chat.finish_open()
            wait_until(lambda: any(call[0] == "export" for call in chat.calls))
            picker.assert_called_once()
        self.assertEqual([call[1] for call in chat.calls if call[0] == "export"], ["old"])

    def test_failed_row_history_load_never_executes_or_replays_queued_actions(self):
        for keyword in ("查找", "导出", "清空"):
            with self.subTest(action=keyword):
                chat = DeferredHistoryChat()
                other = make_panel(chat)
                self.extra_panels.append(other)
                with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as picker, \
                        patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
                    click_row_action(other, "old", keyword)
                    wait_until(lambda: chat.history_loading)
                    chat.finish_open(error="合成档案读取错误")
                    wait_js(other, "Boolean(document.querySelector('.error-banner'))")
                    self.assertEqual(chat.current_conversation_id, "current")
                    self.assertFalse(eval_js(other, "Boolean(document.querySelector('[data-testid=message-search]'))"))
                    picker.assert_not_called()
                    confirm.assert_not_called()
                    chat.open_conversation("old")
                    chat.finish_open()
                    wait_js(other, "document.querySelector('.header-title')?.textContent === '旧会话'")
                    QTest.qWait(80)
                    picker.assert_not_called()
                    confirm.assert_not_called()
                    self.assertFalse(eval_js(other, "Boolean(document.querySelector('[data-testid=message-search]'))"))
                    self.assertFalse(any(call[0] in {"clear", "export"} for call in chat.calls))

    def test_switching_or_creating_conversation_cancels_pending_row_action(self):
        for navigation in ("switch", "new"):
            with self.subTest(navigation=navigation):
                chat = DeferredHistoryChat()
                other = make_panel(chat)
                self.extra_panels.append(other)
                with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as picker:
                    click_row_action(other, "old", "导出")
                    wait_until(lambda: chat.history_loading)
                    if navigation == "new":
                        chat.new_conversation()
                    else:
                        chat.open_conversation("current")
                        chat.finish_open()
                    wait_js(other, "document.querySelector('.header-title')?.textContent === " +
                            json.dumps("新对话" if navigation == "new" else "开发桌宠"))
                    picker.assert_not_called()
                    chat.open_conversation("old")
                    chat.finish_open()
                    wait_js(other, "document.querySelector('.header-title')?.textContent === '旧会话'")
                    QTest.qWait(80)
                    picker.assert_not_called()
                    self.assertFalse(any(call[0] == "export" for call in chat.calls))

    def test_coalesced_external_return_to_origin_cancels_queued_row_export(self):
        chat = DeferredHistoryChat()
        other = make_panel(chat)
        self.extra_panels.append(other)
        snapshots = []
        other.bridge.stateChanged.connect(lambda payload: snapshots.append(json.loads(payload)))
        original_open = chat.open_conversation

        def open_then_immediately_restore_origin(conversation_id, focus_message_id=None):
            # These signals share one Qt/WebChannel callback. The publish timer
            # sees only the final current-session snapshot, never old/loading.
            accepted = original_open(conversation_id, focus_message_id)
            if accepted:
                original_open("current")
                chat.finish_open()
            return accepted

        with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as picker:
            with patch.object(chat, "open_conversation", side_effect=open_then_immediately_restore_origin):
                click_row_action(other, "old", "导出")
                wait_until(lambda: ("open", "old") in chat.calls and ("open", "current") in chat.calls)
                wait_until(lambda: any(item["conversationId"] == "current" and not item["historyLoading"]
                                       for item in snapshots))
                QTest.qWait(80)
                self.assertEqual(chat.current_conversation_id, "current")
                self.assertFalse(any(item["conversationId"] == "old" for item in snapshots), snapshots)
                picker.assert_not_called()
            # Opening the target normally later must not replay the old intent.
            chat.open_conversation("old")
            chat.finish_open()
            wait_js(other, "document.querySelector('.header-title')?.textContent === '旧会话'")
            QTest.qWait(80)
            picker.assert_not_called()
            self.assertFalse(any(call[0] == "export" for call in chat.calls))

    def test_rejected_row_open_does_not_export_the_still_active_conversation(self):
        with patch.object(self.chat, "open_conversation", return_value=False), \
                patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as picker:
            click_row_action(self.panel, "old", "导出")
            wait_js(self.panel, "!document.querySelector('.row-popover')")
            QTest.qWait(80)
            self.assertEqual(self.chat.current_conversation_id, "current")
            picker.assert_not_called()
            self.assertFalse(any(call[0] == "export" for call in self.chat.calls))

    def test_narrow_history_overlay_closes_and_returns_keyboard_focus(self):
        self.panel.resize(640, 720)
        wait_js(self.panel, "document.querySelector('.sidebar').getBoundingClientRect().right <= 1")
        eval_js(self.panel, "document.querySelector('[data-testid=history-toggle]').focus()")
        click_dom(self.panel, "button[aria-label='展开历史会话']")
        wait_js(self.panel, "document.querySelector('.sidebar').getBoundingClientRect().left >= -1")
        self.assertTrue(eval_js(self.panel, "Boolean(document.querySelector('.sidebar-backdrop'))"))
        self.chat.conversations = [{"id": f"row-{index}", "title": f"用于滚动菜单检查的会话 {index}"}
                                   for index in range(30)]
        self.chat.conversations_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('.conversation-row').length === 30")
        eval_js(self.panel, "(() => {const rows=document.querySelectorAll('.conversation-row'); rows[rows.length-1].scrollIntoView({block:'end'}); rows[rows.length-1].querySelector('.row-actions button').click(); return true;})()")
        wait_js(self.panel, "Boolean(document.querySelector('.row-popover'))")
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.row-popover button').length"), 5)
        self.assertTrue(eval_js(self.panel, """(() => {
          const menu=document.querySelector('.row-popover'), rect=menu.getBoundingClientRect();
          const hit=document.elementFromPoint(rect.left+10,rect.top+10);
          return rect.left>=0 && rect.top>=0 && rect.right<=innerWidth+1 && rect.bottom<=innerHeight+1 && Boolean(hit?.closest('.row-popover'));
        })()"""))
        eval_js(self.panel, "window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        wait_js(self.panel, "document.querySelector('.sidebar').getBoundingClientRect().right <= 1")
        wait_js(self.panel, "document.activeElement === document.querySelector('[data-testid=history-toggle]')")
        self.assertEqual(self.chat.current_conversation_id, "current")

    def test_reading_position_stays_until_user_requests_latest(self):
        self.chat.messages = [{"id": f"scroll-{index}", "role": "assistant",
                               "content": f"第 {index + 1} 条记录。" + "可以继续阅读这一段内容。" * 15}
                              for index in range(25)]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('[data-message-id]').length === 25")
        wait_js(self.panel, "document.querySelector('.thread-viewport').scrollHeight > 2000")
        # A consumed focus request must not replay when later messages arrive.
        self.chat.message_focus_requested.emit("scroll-10")
        wait_js(self.panel, """(() => {
          const view=document.querySelector('.thread-viewport'), frame=view.getBoundingClientRect();
          const target=document.querySelector('[data-message-id=scroll-10]').getBoundingClientRect();
          const visible=Math.max(0,Math.min(target.bottom,frame.bottom)-Math.max(target.top,frame.top));
          return visible>=Math.min(target.height,frame.height)*.85 && view.scrollTop>200 && view.scrollHeight-view.scrollTop-view.clientHeight>50;
        })()""")
        eval_js(self.panel, "(() => {const view=document.querySelector('.thread-viewport'); view.scrollTo({top:120,behavior:'instant'}); view.dispatchEvent(new Event('scroll')); return true;})()")
        wait_js(self.panel, "Boolean(document.querySelector('.latest-button'))")
        self.chat.messages.append({"id": "scroll-new", "role": "assistant", "content": "新增回复不会打断正在阅读的历史位置。"})
        self.chat.messages_changed.emit()
        wait_js(self.panel, "Boolean(document.querySelector('[data-message-id=scroll-new]'))")
        self.assertAlmostEqual(eval_js(self.panel, "document.querySelector('.thread-viewport').scrollTop"), 120, delta=2)
        click_dom(self.panel, ".latest-button")
        wait_js(self.panel, "(() => {const view=document.querySelector('.thread-viewport'); return view.scrollHeight-view.scrollTop-view.clientHeight < 3;})()")

    def test_export_bridge_captures_session_before_native_picker(self):
        def choose(*args):
            self.chat.new_conversation()
            return "qa-export-not-written", "JSON 对话 (*.json)"
        with patch.object(QFileDialog, "getSaveFileName", side_effect=choose):
            result = bridge_action(self.panel, "export", conversationId="current")
        self.assertTrue(result["ok"])
        self.assertEqual(self.chat.calls[-1], ("export", "current", "qa-export-not-written.json", "json"))
        self.assertEqual(self.chat.current_conversation_id, "new-session")


if __name__ == "__main__":
    unittest.main()

