"""Compact activity feed through the shipped React/Qt bridge, synthetic local data only."""
from copy import deepcopy
from pathlib import Path
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-gpu-compositing")

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.test_chat_execution_ui import ExecutionChat, save_rendered_snapshot
from tests.test_assistant_ui import click_dom, eval_js, make_panel, set_dom_text, wait_js

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "activity-feed-ui"


def activity(number, label, detail="", *, kind="tool", status="completed"):
    return {"id": f"activity-{number}", "elapsed_ms": number * 1500,
            "kind": kind, "label": label, "detail": detail, "status": status}


def sample_events():
    return [activity(0, "正在分派执行器", kind="progress"),
            activity(1, "我会先检查当前窗口，再核对文件内容。", kind="commentary"),
            activity(2, "查看画面", "应用：文件资源管理器", kind="computer"),
            activity(3, "切换窗口", "应用：记事本", kind="computer"),
            activity(4, "读取文件", "文件：使用说明.md"),
            activity(5, "读取网页", "网站：example.org"),
            activity(6, "已找到配置文件，接下来检查修改后的内容。", kind="commentary"),
            activity(7, "修改文件", "文件：设置.json"),
            activity(8, "运行命令"),
            activity(9, "运行命令"),
            activity(10, "运行命令"),
            activity(11, "检查文件", "文件：设置.json", status="running")]


class ActivityFeedUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def panel(self, events=None):
        chat = ExecutionChat("running")
        chat.agent_approval_mode = "auto"
        chat.set_agent_approval_mode = lambda _mode: False
        chat.messages[0]["content"] = "帮我检查桌面应用，并整理一下使用说明。"
        chat.messages[0]["metadata"]["execution"]["events"] = deepcopy(events if events is not None else sample_events())
        panel = make_panel(chat)
        self.panels.append(panel)
        wait_js(panel, "Boolean(document.querySelector('[data-testid=execution-content]'))")
        return chat, panel

    def test_concrete_actions_and_objects_are_compact_without_timeline_or_step_counters(self):
        _chat, panel = self.panel([activity(1, "读取文件", "文件：使用说明.md"),
            activity(2, "操作电脑", "第 64 步", kind="computer"),
            activity(3, "读取网页", "网站：example.org", status="running"),
            activity(4, "分派任务", "子任务：核对资料")])
        text = eval_js(panel, "document.querySelector('[data-testid=execution-content]').innerText")
        self.assertIn("使用说明.md", text)
        self.assertIn("example.org", text)
        self.assertIn("核对资料", text)
        self.assertNotIn("子任务：", text)
        self.assertNotIn("第 64 步", text)
        self.assertNotIn("+00:", text)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event time,.execution-timeline,.execution-heading').length"), 0)
        self.assertTrue(eval_js(panel, "[...document.querySelectorAll('.execution-event')].every(row=>row.getBoundingClientRect().height<60)"))
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('.event-running .spin'))"))
        self.assertEqual(eval_js(panel, "document.querySelector('.event-running .execution-marker').getAttribute('aria-label')"), "进行中")

    def test_seventy_legacy_repeats_collapse_without_losing_the_failed_attempt(self):
        events = [activity(i, "操作电脑", f"第 {i} 步", kind="computer") for i in range(1, 71)]
        events.append(activity(71, "操作电脑", "第 71 步", kind="computer", status="failed"))
        _chat, panel = self.panel(events)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event').length"), 2)
        self.assertIn("70 次", eval_js(panel, "document.querySelector('[data-testid=execution-group-toggle]').innerText"))
        self.assertIn("未完成", eval_js(panel, "document.querySelector('.event-failed').innerText"))
        self.assertIn("71项操作", eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').innerText"))
        click_dom(panel, "[data-testid=execution-group-toggle]")
        wait_js(panel, "document.querySelectorAll('.execution-group-items .execution-event').length===70")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.event-failed').length"), 1)
        click_dom(panel, "[data-testid=execution-group-toggle]")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event').length"), 2)

    def test_earlier_failure_is_reported_and_earlier_running_task_stays_visible(self):
        events = [activity(0, "执行子任务", "任务：核对资料", status="running"),
                  activity(1, "读取文件", "文件：缺失资料.txt", status="failed")]
        events.extend(activity(i, "读取文件", f"文件：说明-{i}.md") for i in range(2, 23))
        _chat, panel = self.panel(events)
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('[data-activity-id=activity-0] .spin'))"))
        self.assertFalse(eval_js(panel, "Boolean(document.querySelector('[data-activity-id=activity-1]'))"))
        self.assertIn("1 项未完成", eval_js(panel, "document.querySelector('[data-testid=execution-earlier-toggle]').innerText"))
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event').length"), 11)
        click_dom(panel, "[data-testid=execution-earlier-toggle]")
        wait_js(panel, "Boolean(document.querySelector('[data-activity-id=activity-1]'))")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-activity-id=activity-0]').length"), 1)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-event').length"), 23)

    def test_public_commentary_is_plain_text_and_not_counted_as_operations(self):
        events = [activity(0, "公开进展：已找到需要检查的文件。", kind="commentary"),
                  activity(1, "读取文件", "文件：说明.md"),
                  activity(2, "<script>window.FAKE_EXECUTED=true</script>", kind="commentary")]
        events[0]["reasoning"] = "PRIVATE_REASONING_PAYLOAD"
        _chat, panel = self.panel(events)
        wait_js(panel, "document.querySelectorAll('[data-testid=execution-commentary]').length===2")
        self.assertIn("1项操作", eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').innerText"))
        self.assertNotIn("PRIVATE_REASONING_PAYLOAD", eval_js(panel, "document.body.innerText"))
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-commentary script').length"), 0)
        self.assertFalse(eval_js(panel, "Boolean(window.FAKE_EXECUTED)"))

    def test_completion_collapses_and_draft_editor_survives_activity_updates(self):
        chat, panel = self.panel()
        set_dom_text(panel, "#message-input", "稍后补充的要求")
        eval_js(panel, "(() => {window.qaDraft=document.querySelector('#message-input'); return true;})()")
        trace = chat.messages[0]["metadata"]["execution"]
        trace["events"].append(activity(12, "检查文件", "文件：使用说明.md", status="running"))
        chat.messages_changed.emit()
        wait_js(panel, "Boolean(document.querySelector('[data-activity-id=activity-12]'))")
        self.assertEqual(eval_js(panel, "document.querySelector('#message-input').value"), "稍后补充的要求")
        self.assertTrue(eval_js(panel, "window.qaDraft===document.querySelector('#message-input')"))
        click_dom(panel, "[data-testid=execution-toggle]")
        trace["duration_ms"] += 10
        chat.messages_changed.emit()
        QTest.qWait(60)
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')"), "false")
        click_dom(panel, "[data-testid=execution-toggle]")
        trace["state"] = "completed"
        for event in trace["events"]:
            event["status"] = "completed"
        chat.busy = False
        chat.messages[0]["metadata"]["request_status"] = "complete"
        chat.messages_changed.emit()
        wait_js(panel, "document.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded')==='false'")
        click_dom(panel, "[data-testid=execution-toggle]")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=execution-content]'))")
        self.assertFalse(eval_js(panel, "Boolean(document.querySelector('.execution-record .spin'))"))
        self.assertEqual(eval_js(panel, "document.querySelector('#message-input').value"), "稍后补充的要求")

    def test_rendered_light_dark_and_narrow_activity_feed(self):
        _chat, panel = self.panel()
        ARTIFACTS.mkdir(exist_ok=True, parents=True)
        theme = {"value": "light"}
        original = panel.web_state
        with patch.object(panel, "web_state", side_effect=lambda: {**original(), "theme": theme["value"]}):
            for mode in ("light", "dark"):
                theme["value"] = mode
                panel._queue_web_state()
                wait_js(panel, f"document.documentElement.dataset.theme==='{mode}'")
                for width in (1000, 375):
                    panel.resize(width, 820)
                    self.app.processEvents()
                    wait_js(panel, f"innerWidth==={width}")
                    QTest.qWait(300)
                    self.assertFalse(eval_js(panel, "document.documentElement.scrollWidth>innerWidth"))
                    self.assertFalse(eval_js(panel, "document.querySelector('[data-testid=stop-button]').disabled"))
                    self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-record time').length"), 0)
                    save_rendered_snapshot(panel, ARTIFACTS / f"activity-{mode}-{width}.png")

    def test_empty_running_avatar_is_hidden_but_real_stream_and_interruption_still_render(self):
        chat, panel = self.panel()
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.message-assistant').length"), 0)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.execution-user-result .assistant-avatar').length"), 1)
        preview = {"conversationId": "current", "requestId": "local-feed-preview", "text": "已经核对文件内容。", "state": "streaming"}
        original = panel.web_state
        with patch.object(panel, "web_state", side_effect=lambda: {**original(), "streamingReply": preview}):
            panel._queue_web_state()
            wait_js(panel, "Boolean(document.querySelector('[data-stream-state=streaming]'))")
            self.assertEqual(eval_js(panel, "document.querySelectorAll('.message-assistant').length"), 1)
            self.assertIn("已经核对文件内容", eval_js(panel, "document.querySelector('.message-assistant').innerText"))
            self.assertTrue(eval_js(panel, "Boolean(document.querySelector('.streaming-indicator'))"))
            preview["state"] = "interrupted"
            panel._queue_web_state()
            wait_js(panel, "Boolean(document.querySelector('[data-stream-state=interrupted]'))")
            self.assertIn("未完成", eval_js(panel, "document.querySelector('.message-assistant').innerText"))
        chat.busy = False
        chat.messages.append({"id": "empty-final", "role": "assistant", "content": "", "metadata": {"request_status": "failed"}})
        chat.messages_changed.emit()
        wait_js(panel, "Boolean(document.querySelector('[data-message-id=empty-final]'))")


if __name__ == "__main__":
    unittest.main(verbosity=2)
