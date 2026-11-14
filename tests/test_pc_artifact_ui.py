"""Reply-owned deliveries through the shipped Qt/WebChannel/React surface."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-gpu-compositing")

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.agent_artifacts import public_artifacts
from tests.test_assistant_ui import AssistantUiChat, click_dom, eval_js, make_panel, set_dom_text, wait_js, wait_until
from tests.test_chat_execution_ui import save_rendered_snapshot


ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "pc-artifact-ui"


def artifact(letter="a", name="同学会.txt", owner="answer-original"):
    item = {"reference": "artifact_" + letter * 64, "name": name, "mime": "text/plain",
            "bytes": 4096, "sha256": "c" * 64, "verification_scope": "immutable_delivery",
            "source_path": "PRIVATE_PATH_MUST_NOT_APPEAR"}
    if owner is not None:
        item["message_id"] = owner
    return item


def message(identifier, role, text, source=None):
    value = {"id": identifier, "role": role, "content": text, "source": source or role}
    if source == "proactive_screen":
        value["screen_event"] = {"app_name": "演示窗口", "event_type": "media",
                                 "context_summary": "测试画面", "observed_at": "刚刚"}
    return value


class ArtifactChat(AssistantUiChat):
    def __init__(self):
        super().__init__()
        self.current_title = "千字搞笑小说《同学会》"
        self.messages = [
            message("request-original", "user", "帮我写一个同学会故事，保存成文件。"),
            message("answer-original", "assistant", "故事已经写好，文件放在这条回复下面。"),
            message("request-next", "user", "打开抖音"),
            message("answer-next", "assistant", "抖音已打开。"),
            message("screen-next", "assistant", "这个发光的透明舱有点神秘。", "proactive_screen"),
        ]
        self.agent_artifacts = [artifact()]
        self.exports = []

    def export_agent_artifact(self, reference, path, *, conversation_id=None):
        self.exports.append((reference, path, conversation_id))
        return conversation_id == self.current_conversation_id


class PublicArtifactOwnershipTests(unittest.TestCase):
    def test_owner_survives_the_host_projection_without_local_paths(self):
        source = artifact()
        public = public_artifacts([source, deepcopy(source)])
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]["message_id"], "answer-original")
        self.assertNotIn("PRIVATE_PATH", json.dumps(public))
        for invalid in ("../other", "C:/private", "line\nbreak", [], "a" * 201):
            with self.subTest(owner=invalid):
                projected = public_artifacts([dict(source, message_id=invalid)])
                self.assertEqual(len(projected), 1)
                self.assertNotIn("message_id", projected[0])


class PcArtifactUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panels = []

    def panel(self, chat=None):
        chat = chat or ArtifactChat()
        panel = make_panel(chat)
        self.panels.append(panel)
        wait_js(panel, "Boolean(document.querySelector('[data-testid=agent-artifact]'))")
        return chat, panel

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def owner(self, panel, letter="a"):
        return eval_js(panel, "document.querySelector('[data-artifact-reference=artifact_" + letter * 64 +
                       "]')?.closest('[data-message-id]')?.dataset.messageId || null")

    def test_delivery_stays_inside_its_reply_before_later_chat_and_screen_comments(self):
        chat, panel = self.panel()
        self.assertEqual(panel.web_state()["agentArtifacts"][0]["message_id"], "answer-original")
        self.assertEqual(self.owner(panel), "answer-original")
        self.assertTrue(eval_js(panel, """(() => {
            const body=document.querySelector('[data-message-id=answer-original]');
            const text=body.querySelector('.markdown').getBoundingClientRect();
            const card=body.querySelector('[data-testid=agent-artifact]').getBoundingClientRect();
            const next=document.querySelector('[data-message-id=request-next]').getBoundingClientRect();
            return text.bottom<=card.top && card.bottom<=next.top;
        })()"""))
        set_dom_text(panel, "#message-input", "下一条草稿不能丢失")
        eval_js(panel, "(() => {window.pcDraft=document.querySelector('#message-input'); return true;})()")
        chat.messages.extend([
            message("request-later", "user", "继续聊天"),
            message("screen-later", "assistant", "又出现了新的画面。", "proactive_screen")])
        chat.messages_changed.emit()
        wait_js(panel, "Boolean(document.querySelector('[data-message-id=screen-later]'))")
        self.assertEqual(self.owner(panel), "answer-original")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=agent-artifact]').length"), 1)
        self.assertEqual(eval_js(panel, "document.querySelector('#message-input').value"), "下一条草稿不能丢失")
        self.assertTrue(eval_js(panel, "window.pcDraft===document.querySelector('#message-input')"))

    def test_legacy_unique_name_ignores_screen_comments_and_unknown_owner_stays_at_history_top(self):
        chat = ArtifactChat()
        chat.messages[1]["content"] = "已保存同学会.txt。"
        chat.messages[-1]["content"] = "你刚刚打开了同学会.txt。"
        chat.agent_artifacts = [artifact(owner=None), artifact("b", "更早的文件.txt", "unloaded-answer")]
        _chat, panel = self.panel(chat)
        self.assertEqual(self.owner(panel), "answer-original")
        self.assertIsNone(self.owner(panel, "b"))
        self.assertTrue(eval_js(panel, "Boolean(document.querySelector('.earlier-deliveries [data-artifact-reference=artifact_" + "b" * 64 + "]'))"))
        self.assertTrue(eval_js(panel, """document.querySelector('.earlier-deliveries').getBoundingClientRect().bottom
            <= document.querySelector('[data-message-id=request-original]').getBoundingClientRect().top"""))
        chat.messages.append(message("answer-mentions-file", "assistant", "同学会.txt 仍然保留。"))
        chat.messages_changed.emit()
        wait_js(panel, "Boolean(document.querySelector('[data-message-id=answer-mentions-file]'))")
        # No owner metadata and two ordinary replies mention it: never guess the newest reply.
        self.assertIsNone(self.owner(panel))
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.earlier-deliveries [data-testid=agent-artifact]').length"), 2)

    def test_same_named_deliveries_stay_separate_and_save_uses_clicked_reference(self):
        chat = ArtifactChat()
        chat.agent_artifacts = [artifact(), artifact("b", owner="answer-next")]
        _chat, panel = self.panel(chat)
        self.assertEqual(self.owner(panel), "answer-original")
        self.assertEqual(self.owner(panel, "b"), "answer-next")
        self.assertEqual(chat.exports, [])
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=("C:/Selected/story.txt", "")) as dialog:
            click_dom(panel, "[data-artifact-reference=artifact_" + "b" * 64 + "] button")
            wait_until(lambda: bool(chat.exports))
            dialog.assert_called_once()
        self.assertEqual(chat.exports, [("artifact_" + "b" * 64, "C:/Selected/story.txt", "current")])
        wait_js(panel, "Boolean(document.querySelector('[data-message-id=answer-next] [data-testid=agent-artifact-result]'))")
        self.assertIsNone(eval_js(panel, "document.querySelector('[data-message-id=answer-original] [data-testid=agent-artifact-result]')?.textContent || null"))

    def test_rendered_reply_delivery_in_light_dark_and_narrow_windows(self):
        _chat, panel = self.panel()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        mode = {"value": "light"}
        original = panel.web_state
        with patch.object(panel, "web_state", side_effect=lambda: {**original(), "theme": mode["value"]}):
            for theme, width in (("light", 1000), ("dark", 1000), ("light", 375)):
                mode["value"] = theme
                panel.resize(width, 960)
                panel._queue_web_state()
                wait_js(panel, f"document.documentElement.dataset.theme==='{theme}' && innerWidth==={width}")
                eval_js(panel, "(() => {document.querySelector('.thread-viewport').scrollTop=0; return true;})()")
                QTest.qWait(350)
                self.assertEqual(self.owner(panel), "answer-original")
                self.assertFalse(eval_js(panel, "document.documentElement.scrollWidth>innerWidth"))
                save_rendered_snapshot(panel, ARTIFACTS / f"reply-delivery-{theme}-{width}.png")

    def test_save_dialog_has_one_owner_across_cards_and_releases_after_cancel(self):
        chat = ArtifactChat()
        chat.agent_artifacts = [artifact(), artifact("b", owner="answer-next")]
        _chat, panel = self.panel(chat)
        nested = []
        def choose(*_args):
            nested.append(panel._open_artifact_result({"conversationId": "current", "reference": "artifact_" + "b" * 64}))
            return "", ""
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", side_effect=choose) as dialog:
            cancelled = panel._open_artifact_result({"conversationId": "current", "reference": "artifact_" + "a" * 64})
            dialog.assert_called_once()
        self.assertEqual(nested, [{"ok": False, "status": "busy"}])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(chat.exports, [])
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=("C:/Selected/after-cancel.txt", "")):
            result = panel._open_artifact_result({"conversationId": "current", "reference": "artifact_" + "b" * 64})
        self.assertEqual(result, {"ok": True, "status": "saved"})
        self.assertEqual(chat.exports, [("artifact_" + "b" * 64, "C:/Selected/after-cancel.txt", "current")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
