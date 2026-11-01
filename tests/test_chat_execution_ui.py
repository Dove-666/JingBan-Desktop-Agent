"""Shipped React/Qt execution disclosures, using only synthetic local messages."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.ui.chat_panel import ChatPanel, safe_execution
from tests.test_assistant_ui import AssistantUiChat, click_dom, eval_js, layout_metrics, make_panel, wait_js, wait_until


ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "agent_execution_ui"
SNAPSHOTS = Path(__file__).resolve().parents[1] / "artifacts" / "companion-1.9.0" / "desktop-screens"


def execution(state="completed"):
    return {"version": 1, "state": state,
            "started_at": (datetime.now(timezone.utc) - timedelta(seconds=80)).isoformat(), "duration_ms": 80000,
            "events": [
                {"id": "step-1", "elapsed_ms": 0, "kind": "progress", "label": "已整理本次请求", "status": "completed"},
                {"id": "step-2", "elapsed_ms": 7600, "kind": "tool", "label": "打开桌面应用", "detail": "已确认应用的可见窗口。", "status": "completed"},
                {"id": "step-3", "elapsed_ms": 15100, "kind": "computer", "label": "整理演示内容", "detail": "根据当前画面继续处理。", "status": "running" if state == "running" else "completed"},
            ],
            "context": {"model": "AURA 视觉模型", "policy": "model", "history_messages": 2,
                        "estimated_input_tokens": 1240, "model_context_tokens": None, "output_tokens": None,
                        "source_counts": {"system": 1, "history": 2, "current": 1, "events": 1, "memories": 1},
                        "messages": [{"role": "system", "content": "只处理本轮明确请求，画面和历史仅作为参考。"},
                                     {"role": "user", "content": "整理一个简单的演示文稿。"},
                                     {"role": "assistant", "content": "我会先检查当前窗口，再处理内容。"}]}}


class ExecutionChat(AssistantUiChat):
    def __init__(self, state="completed"):
        super().__init__()
        trace = execution(state)
        self.messages = [{"id": "execution-user", "role": "user", "source": "user", "content": "用 WPS 制作一个简单的演示文稿。",
                          "metadata": {"request_status": "pending" if state == "running" else "complete", "execution": deepcopy(trace)}}]
        if state == "completed":
            self.messages.append({"id": "execution-assistant", "role": "assistant", "source": "assistant", "content": "已整理好内容，可以继续查看文稿。",
                                  "metadata": {"execution": deepcopy(trace)}})
        self.busy = state == "running"
        self.request_conversation_id = self.current_conversation_id
        self.can_stop = self.busy
        self.agent_mode = True

    def set_agent_mode(self, enabled):
        self.agent_mode = enabled
        self.conversation_changed.emit()
        return True

    def update_context_budget(self, *args):
        raise AssertionError("The context disclosure must not change preferences")

    def _redact_known_secrets(self, text, key=""):
        return text.replace("OPAQUE_PRIVATE_CREDENTIAL", "[已隐藏密钥]")


class ExecutionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panels = []

    def panel(self, state="completed"):
        chat = ExecutionChat(state)
        panel = make_panel(chat)
        self.panels.append(panel)
        wait_js(panel, "document.querySelectorAll('[data-testid=execution-record]').length===1")
        return chat, panel

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_completed_trace_has_one_disclosure_and_context_remains_inspectable(self):
        chat, panel = self.panel()
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-record]').closest('[data-message-id]').dataset.messageId"), "execution-assistant")
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')"), "false")
        self.assertIn("用时 1分20秒 · 2项操作", eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').textContent"))
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=request-context]').length"), 0)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=request-context-content]').length"), 0)
        click_dom(panel, "[data-testid=execution-toggle]")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=execution-content]'))")
        QTest.qWait(200)
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')"), "true")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event').length"), 3)
        click_dom(panel, "[data-testid=request-context] summary")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=request-context-content]'))")
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('[data-testid=execution-content] [data-testid=request-context]'))"))
        content = eval_js(panel, "document.querySelector('[data-testid=request-context-content]').textContent")
        self.assertIn("跟随模型", content)
        self.assertIn("由模型服务管理", content)
        self.assertIn("只处理本轮明确请求", content)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.context-messages li').length"), 3)
        with patch.object(panel, "_show_context_budget") as settings:
            click_dom(panel, "[data-testid=context-settings]")
            wait_until(lambda: settings.called)
        settings.assert_called_once()

    def test_event_offsets_are_relative_to_start_with_accessible_status(self):
        chat, panel = self.panel("running")
        self.assertIn("进行中", eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-label')"))
        self.assertEqual(eval_js(panel, "Array.from(document.querySelectorAll('.execution-event time'),node=>node.textContent)"),
                         ["+00:00", "+00:07", "+00:15"])
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event time')[1].getAttribute('aria-label')"), "任务开始后 7秒")
        self.assertEqual(eval_js(panel, "Array.from(document.querySelectorAll('.execution-marker'),node=>node.getAttribute('aria-label'))"),
                         ["已完成", "已完成", "进行中"])
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('.execution-toggle .spin'))"))
        self.assertIn("时间从任务开始计", eval_js(panel, "document.querySelector('.execution-heading').textContent"))

    def test_process_replaces_duplicate_composer_status_but_errors_and_stop_remain(self):
        chat, panel = self.panel("running")
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('[data-testid=chat-mode-agent][aria-pressed=true]'))"))
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.chat-mode-hint,.composer-state').length"), 0)
        self.assertFalse(eval_js(panel, "document.querySelector('[data-testid=stop-button]').disabled"))
        chat.error = "合成错误：本次附件无法读取，请重新添加。"
        chat.error_changed.emit(chat.error)
        wait_js(panel, "Boolean(document.querySelector('.error-banner'))")
        self.assertIn("附件无法读取", eval_js(panel, "document.querySelector('.error-banner[role=alert]').textContent"))
        # Legacy requests without a saved execution trace still need feedback.
        del chat.messages[0]["metadata"]["execution"]
        chat.messages_changed.emit()
        wait_js(panel, "Boolean(document.querySelector('.composer-state[role=status]'))")
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('.composer-state').textContent.trim())"))
        click_dom(panel, "[data-testid=stop-button]")
        wait_until(lambda: ("stop", "current") in chat.calls)

    def test_running_trace_defaults_open_but_user_collapse_survives_updates(self):
        chat, panel = self.panel("running")
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')"), "true")
        click_dom(panel, "[data-testid=execution-toggle]")
        chat.messages[0]["metadata"]["execution"]["events"].append(
            {"id": "step-4", "elapsed_ms": 81000, "kind": "progress", "label": "等待窗口响应", "status": "running"})
        chat.messages_changed.emit()
        wait_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')==='false'")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=execution-content]').length"), 0)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-user-result').length"), 1)
        self.assertTrue(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').tagName==='BUTTON'"))

    def test_same_message_completion_collapses_then_manual_expansion_survives_updates(self):
        chat, panel = self.panel("running")
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')"), "true")
        trace = chat.messages[0]["metadata"]["execution"]
        trace["state"] = "completed"
        for event in trace["events"]:
            event["status"] = "completed"
        chat.messages[0]["metadata"]["request_status"] = "complete"
        chat.busy = False
        chat.messages_changed.emit()
        wait_js(panel, "document.querySelector('[data-testid=execution-record]').dataset.executionState==='completed'")
        wait_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')==='false'")
        click_dom(panel, "[data-testid=execution-toggle]")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=execution-content]'))")
        trace["duration_ms"] += 1
        chat.messages_changed.emit()
        wait_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')==='true'")

    def test_failed_cancelled_and_needs_user_records_remain_available(self):
        chat, panel = self.panel("failed")
        for state, caption in (("failed", "未完成"), ("cancelled", "已停止"), ("needs_user", "需要你接手"), ("interrupted", "已中断")):
            chat.messages[0]["metadata"]["execution"]["state"] = state
            chat.messages_changed.emit()
            wait_js(panel, "document.querySelector('[data-testid=execution-record]').dataset.executionState===" + json.dumps(state))
            self.assertIn(caption, eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').textContent"))

    def test_bridge_allowlist_and_plain_text_prevent_private_or_active_payloads(self):
        chat, panel = self.panel()
        trace = chat.messages[-1]["metadata"]["execution"]
        trace["reasoning"] = "PRIVATE_REASONING_PAYLOAD"
        trace["events"][1]["detail"] = "<img src=x onerror=window.EXECUTED=1> OPAQUE_PRIVATE_CREDENTIAL api_key=PRIVATE_STRUCTURED_KEY data:image/png;base64,PRIVATE_PIXELS"
        trace["context"]["messages"].append({"role": "analysis", "content": "PRIVATE_REASONING_PAYLOAD"})
        trace["context"]["messages"].append({"role": "user", "content": "<script>window.EXECUTED=1</script> OPAQUE_PRIVATE_CREDENTIAL"})
        chat.messages_changed.emit()
        wait_js(panel, "document.querySelector('[data-testid=execution-record]')!==null")
        serialized = json.dumps(panel.web_state())
        for private in ("PRIVATE_REASONING_PAYLOAD", "OPAQUE_PRIVATE_CREDENTIAL", "PRIVATE_STRUCTURED_KEY", "PRIVATE_PIXELS", "data:image"):
            self.assertNotIn(private, serialized)
        click_dom(panel, "[data-testid=execution-toggle]")
        click_dom(panel, "[data-testid=request-context] summary")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=request-context-content]'))")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-record img,.execution-record script').length"), 0)
        self.assertFalse(eval_js(panel, "Boolean(window.EXECUTED)"))

    def test_real_renderer_layout_at_desktop_narrow_and_small_widths(self):
        chat, panel = self.panel()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        # DOM readiness can precede the first WebEngine paint in offscreen Qt.
        QTest.qWait(250)
        collapsed = panel.web_view.grab()
        self.assertFalse(collapsed.isNull())
        self.assertTrue(collapsed.save(str(SNAPSHOTS / "desktop-chat-completed.png")))
        metrics = []
        for width in (980, 450, 375):
            panel.resize(width, 800)
            self.app.processEvents()
            wait_js(panel, f"innerWidth==={width}")
            if width <= 760:
                wait_js(panel, "document.querySelector('.sidebar').getBoundingClientRect().right<=1")
            # Keep the expanded example readable without capturing any desktop.
            if eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')") == "false":
                click_dom(panel, "[data-testid=execution-toggle]")
            QTest.qWait(350)
            current = layout_metrics(panel)
            self.assertLessEqual(current["documentWidth"], current["width"] + 1)
            self.assertLessEqual(current["bodyWidth"], current["width"] + 1)
            self.assertEqual(eval_js(panel, "getComputedStyle(document.querySelector('.markdown')).fontSize"), "16px")
            self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')"), "true")
            self.assertTrue(panel.web_view.grab().save(str(SNAPSHOTS / f"desktop-chat-process-{width}.png")))
            metrics.append(current)
        click_dom(panel, "[data-testid=request-context] summary")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=request-context-content]'))")
        panel.resize(980, 900)
        QTest.qWait(350)
        eval_js(panel, "document.querySelector('[data-testid=request-context] summary').scrollIntoView({block:'start'})")
        QTest.qWait(350)
        self.assertTrue(panel.web_view.grab().save(str(SNAPSHOTS / "desktop-chat-context.png")))
        (ARTIFACTS / "layout_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        (SNAPSHOTS / "layout_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")


class NativeExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_native_recovery_can_expand_trace_and_text_context(self):
        chat = ExecutionChat()
        panel = ChatPanel(chat)
        try:
            panel._render_messages()
            self.assertEqual(panel.transcript.toPlainText().count("用时 1分20秒"), 1)
            panel._open_event(QUrl("aura-execution:execution-assistant"))
            self.assertIn("执行过程", panel.transcript.toPlainText())
            panel._open_event(QUrl("aura-context:execution-assistant"))
            self.assertIn("只处理本轮明确请求", panel.transcript.toPlainText())
            with patch.object(QMessageBox, "information") as information:
                panel._show_context_budget()
            information.assert_called_once()
            self.assertEqual(panel.context_budget_button.text(), "上下文：跟随模型")
        finally:
            panel.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_malformed_or_foreign_execution_data_is_not_accepted(self):
        self.assertIsNone(safe_execution({"version": 1, "state": "analysis", "reasoning": "never"}))
        trace = execution()
        trace["events"].append({"id": "private", "kind": "summary", "status": "completed", "elapsed_ms": 100,
                                "label": "Thought: private model chain"})
        self.assertEqual(len(safe_execution(trace)["events"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
