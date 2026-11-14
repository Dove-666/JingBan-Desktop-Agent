"""Real WebChannel/DOM and native questions, with synthetic controller state."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from desktop_pet.ui.agent_interaction import public_interaction
from tests.test_assistant_ui import AssistantUiChat, make_panel, eval_js, set_dom_text, click_dom, wait_js, bridge_action


class InteractiveChat(AssistantUiChat):
    agent_interaction_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.busy = True
        self.agent_mode = True
        self.agent_running_conversation_id = self.current_conversation_id
        self.can_stop = True
        self.pending_agent_interaction = None
        self.agent_artifacts = []
        self.exports = []
        self.answers, self.steering = [], []

    def set_agent_mode(self, enabled):
        self.agent_mode = enabled
        return True

    def answer_agent_interaction(self, identifier, answer, conversation_id=None):
        pending = self.pending_agent_interaction
        if pending is None or pending["id"] != identifier or pending["conversation_id"] != conversation_id:
            return False
        self.answers.append((identifier, deepcopy(answer), conversation_id))
        self.pending_agent_interaction = None
        self.agent_interaction_changed.emit(None)
        return True

    def steer_agent(self, text, conversation_id=None):
        if self.pending_agent_interaction or conversation_id != self.agent_running_conversation_id:
            return False
        self.steering.append((text, conversation_id))
        return True

    def export_agent_artifact(self, reference, path, *, conversation_id=None):
        if conversation_id != self.current_conversation_id:
            return False
        self.exports.append((reference, path, conversation_id))
        return True


class AgentInteractionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = InteractiveChat()
        self.panel = make_panel(self.chat)

    def tearDown(self):
        self.panel.hide(); self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def question(self, kind="question"):
        value = {"id": "question-1", "kind": kind, "question": "选择实际需要的文件",
                 "conversation_id": "current", "options": ["A", "B"], "private_context": "NEVER_PUBLIC"}
        if kind == "file_access":
            value["resource"] = {"path": "C:\\Selected\\input.csv", "access": "read", "directory": False}
        self.chat.pending_agent_interaction = value
        self.chat.agent_interaction_changed.emit(value)
        return value

    def test_real_question_options_answer_once_and_private_fields_never_reach_web_state(self):
        self.question()
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-interaction]'))")
        self.assertNotIn("NEVER_PUBLIC", json.dumps(self.panel.web_state()))
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.agent-interaction-options button').length"), 2)
        click_dom(self.panel, ".agent-interaction-options button:nth-child(2)")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-interaction]')")
        self.assertEqual(self.chat.answers, [("question-1", {"text": "B"}, "current")])
        self.assertFalse(bridge_action(self.panel, "answerAgentInteraction", conversationId="current",
            interactionId="question-1", answer={"text": "A"})["ok"])
        self.assertEqual(len(self.chat.answers), 1)

    def test_freeform_answer_and_file_approval_are_distinct_explicit_actions(self):
        self.question()
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-answer-input]'))")
        set_dom_text(self.panel, "[data-testid=agent-answer-input]", "使用刚才选的那份")
        click_dom(self.panel, ".agent-interaction form button")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-interaction]')")
        self.assertEqual(self.chat.answers[-1][1], {"text": "使用刚才选的那份"})
        self.question("file_access")
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-allow-access]'))")
        self.assertIn("C:\\Selected\\input.csv", eval_js(self.panel, "document.querySelector('.agent-interaction').innerText"))
        self.assertFalse(bridge_action(self.panel, "answerAgentInteraction", conversationId="current",
            interactionId="question-1", answer={"allowed": "true"})["ok"])
        click_dom(self.panel, "[data-testid=agent-allow-access]")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-interaction]')")
        self.assertEqual(self.chat.answers[-1][1], {"allowed": True})

    def test_running_composer_steers_without_creating_an_ordinary_chat_turn(self):
        wait_js(self.panel, "document.querySelector('#message-input')?.placeholder === '补充当前任务…'")
        set_dom_text(self.panel, "#message-input", "先别发送，另存一份")
        wait_js(self.panel, "!document.querySelector('[data-testid=send-button]').disabled")
        click_dom(self.panel, "[data-testid=send-button]")
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")
        self.assertEqual(self.chat.steering, [("先别发送，另存一份", "current")])
        self.assertEqual(self.chat.sent, [])

    def test_pending_question_and_foreign_conversation_block_steering(self):
        self.question("file_access")
        self.assertFalse(bridge_action(self.panel, "steerAgent", conversationId="current", text="continue")["ok"])
        self.assertFalse(bridge_action(self.panel, "answerAgentInteraction", conversationId="other",
            interactionId="question-1", answer={"allowed": True})["ok"])
        self.chat.current_conversation_id = "other"
        self.assertIsNone(self.panel.web_state()["agentInteraction"])
        self.assertFalse(self.panel.web_state()["canSteerAgent"])
        self.assertEqual(self.chat.answers, []); self.assertEqual(self.chat.steering, [])

    def test_oversized_steering_is_explained_preserves_draft_and_can_be_corrected(self):
        wait_js(self.panel, "document.querySelector('#message-input')?.placeholder === '补充当前任务…'")
        set_dom_text(self.panel, "#message-input", "字" * 8001)
        wait_js(self.panel, "document.querySelector('[data-testid=steering-feedback]')?.textContent.includes('8001')")
        self.assertTrue(eval_js(self.panel, "document.querySelector('[data-testid=send-button]').disabled"))
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "字" * 8001)
        eval_js(self.panel, "document.querySelector('.composer').requestSubmit()")
        QTest.qWait(100)
        self.assertEqual(self.chat.steering, [])
        self.assertEqual(bridge_action(self.panel, "steerAgent", conversationId="current", text="字" * 8001)["error"],
                         "STEERING_TOO_LONG")
        set_dom_text(self.panel, "#message-input", "字" * 8000)
        wait_js(self.panel, "!document.querySelector('[data-testid=send-button]').disabled")
        click_dom(self.panel, "[data-testid=send-button]")
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")
        self.assertEqual(self.chat.steering, [("字" * 8000, "current")])

    def test_steering_emoji_limit_uses_same_unicode_count_as_host(self):
        wait_js(self.panel, "document.querySelector('#message-input')?.placeholder === '补充当前任务…'")
        text = "🐳" * 4000 + "字" * 4000
        set_dom_text(self.panel, "#message-input", text)
        wait_js(self.panel, "document.querySelector('[data-testid=steering-feedback]')?.textContent.includes('8000 / 8000')")
        self.assertFalse(eval_js(self.panel, "document.querySelector('[data-testid=send-button]').disabled"))
        click_dom(self.panel, "[data-testid=send-button]")
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")
        self.assertEqual(self.chat.steering, [(text, "current")])

    def test_rejected_steering_explains_failure_without_clearing_draft(self):
        wait_js(self.panel, "document.querySelector('#message-input')?.placeholder === '补充当前任务…'")
        set_dom_text(self.panel, "#message-input", "保留这段补充")
        wait_js(self.panel, "!document.querySelector('[data-testid=send-button]').disabled")
        with patch.object(self.chat, "steer_agent", return_value=False):
            click_dom(self.panel, "[data-testid=send-button]")
            wait_js(self.panel, "document.querySelector('[data-testid=steering-feedback]')?.textContent.includes('未发送')")
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "保留这段补充")
        set_dom_text(self.panel, "#message-input", "已修改")
        wait_js(self.panel, "!document.querySelector('[data-testid=steering-feedback]')")

    def test_ordinary_chat_is_not_subject_to_steering_character_limit(self):
        self.chat.busy = False
        self.chat.busy_changed.emit(False)
        self.panel._queue_web_state()
        wait_js(self.panel, "document.querySelector('#message-input')?.placeholder !== '补充当前任务…'")
        set_dom_text(self.panel, "#message-input", "字" * 8001)
        wait_js(self.panel, "!document.querySelector('[data-testid=send-button]').disabled")
        self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('[data-testid=steering-feedback]'))"))

    def test_native_fallback_shows_the_same_question_and_accepts_explicit_denial(self):
        self.question("file_access")
        self.panel._activate_native()
        QTest.qWait(30)
        card = self.panel.agent_interaction_card
        self.assertTrue(card.isVisible())
        button = next(b for b in card.findChildren(QPushButton) if b.text() == "拒绝")
        button.click()
        self.assertEqual(self.chat.answers, [("question-1", {"allowed": False}, "current")])

    def test_native_emoji_answer_preserves_over_limit_draft_and_full_valid_boundary(self):
        self.question()
        self.panel._activate_native()
        QTest.qWait(30)
        card = self.panel.agent_interaction_card
        editor = card.findChild(QLineEdit)
        button = next(item for item in card.findChildren(QPushButton) if item.text() == "继续")
        oversized = "🐳" * 4000 + "字" * 4001
        editor.setText(oversized)
        button.click()
        self.assertTrue(editor.text() == oversized)
        self.assertTrue(card.error.isVisible())
        self.assertIn("8000", card.error.text())
        self.assertTrue(editor.isEnabled())
        self.assertEqual(self.chat.answers, [])
        boundary = "🐳" * 8000
        editor.setText(boundary)
        self.assertTrue(editor.text() == boundary)
        button.click()
        self.assertEqual(len(self.chat.answers), 1)
        self.assertTrue(self.chat.answers[0][1]["text"] == boundary)

    def test_delivery_card_opens_native_save_dialog_only_after_click_with_current_reference(self):
        reference = "artifact_" + "a" * 64
        self.chat.agent_artifacts = [{"reference": reference, "name": "报告.docx", "mime": "application/octet-stream",
            "bytes": 100, "sha256": "b" * 64, "verification_scope": "format_and_readback", "private_path": "NEVER_PUBLIC"}]
        self.chat.agent_interaction_changed.emit(None)
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-artifact]'))")
        self.assertNotIn("NEVER_PUBLIC", json.dumps(self.panel.web_state()))
        self.assertEqual(self.chat.exports, [])
        self.assertFalse(bridge_action(self.panel, "openArtifact", conversationId="other", reference=reference)["ok"])
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=("C:/Selected/report.docx", "")) as dialog:
            click_dom(self.panel, "[data-testid=agent-artifact] button")
            wait_js(self.panel, "!document.querySelector('[data-testid=agent-artifact] button').disabled")
            dialog.assert_called_once()
        self.assertEqual(self.chat.exports, [(reference, "C:/Selected/report.docx", "current")])

    def test_malformed_and_cross_session_interactions_fail_closed(self):
        value = self.question()
        for changes in ({"conversation_id": "other"}, {"kind": []}, {"options": ["x"] * 9},
                        {"kind": "file_access", "resource": {"path": "x", "access": [], "directory": False}}):
            self.assertIsNone(public_interaction({**value, **changes}, "current"))

    def test_host_240_character_option_survives_both_native_and_react_validation(self):
        option = "方案" * 120
        value = self.question()
        value["options"] = [option]
        self.chat.agent_interaction_changed.emit(value)
        self.assertEqual(public_interaction(value, "current")["options"], [option])
        wait_js(self.panel, "document.querySelector('.agent-interaction-options button')?.textContent.length === 240")
        click_dom(self.panel, ".agent-interaction-options button")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-interaction]')")
        self.assertEqual(self.chat.answers[-1][1], {"text": option})
        self.assertIsNone(public_interaction({**value, "options": [option + "x"]}, "current"))

    def test_long_emoji_question_and_option_reach_actual_dom_without_truncation(self):
        question, option = "🐳" * 16000, "🐬" * 240
        value = self.question()
        value.update(question=question, options=[option])
        self.chat.agent_interaction_changed.emit(value)
        wait_js(self.panel, "Array.from(document.querySelector('.agent-interaction > p')?.textContent || '').length === 16000")
        self.assertTrue(eval_js(self.panel, "document.querySelector('.agent-interaction > p').textContent") == question)
        self.assertTrue(eval_js(self.panel, "document.querySelector('.agent-interaction-options button').textContent") == option)
        click_dom(self.panel, ".agent-interaction-options button")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-interaction]')")
        self.assertEqual(self.chat.answers, [("question-1", {"text": option}, "current")])
        self.assertIsNone(public_interaction({**value, "question": question + "字"}, "current"))
        self.assertIsNone(public_interaction({**value, "options": [option + "字"]}, "current"))

    def test_emoji_answer_limit_preserves_oversized_draft_then_accepts_full_boundary(self):
        self.question()
        selector = "[data-testid=agent-answer-input]"
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-answer-input]'))")
        oversized = "🐳" * 4000 + "字" * 4001
        set_dom_text(self.panel, selector, oversized)
        click_dom(self.panel, ".agent-interaction form button")
        wait_js(self.panel, "document.querySelector('.agent-interaction-error')?.textContent.includes('8000')")
        self.assertEqual(self.chat.answers, [])
        self.assertTrue(eval_js(self.panel, "document.querySelector('[data-testid=agent-answer-input]').value") == oversized)
        self.assertFalse(eval_js(self.panel, "document.querySelector('[data-testid=agent-answer-input]').disabled"))
        boundary = "🐳" * 8000
        set_dom_text(self.panel, selector, boundary)
        self.assertTrue(eval_js(self.panel, "document.querySelector('[data-testid=agent-answer-input]').value") == boundary)
        click_dom(self.panel, ".agent-interaction form button")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-interaction]')")
        self.assertEqual(len(self.chat.answers), 1)
        self.assertTrue(self.chat.answers[0][1]["text"] == boundary)

    def test_dark_permission_button_remains_readable_and_long_paths_wrap_at_narrow_width(self):
        value = self.question("file_access")
        value["resource"]["path"] = "C:\\Selected\\" + "资料" * 80 + "\\input.csv"
        original = self.panel.web_state
        with patch.object(self.panel, "web_state", side_effect=lambda: {**original(), "theme": "dark"}):
            self.panel.resize(480, 900)
            self.panel._queue_web_state()
            wait_js(self.panel, "document.documentElement.dataset.theme === 'dark' && Boolean(document.querySelector('[data-testid=agent-allow-access]'))")
            result = eval_js(self.panel, """JSON.stringify((() => {
              const card=document.querySelector('.agent-interaction');
              const style=getComputedStyle(document.querySelector('[data-testid=agent-allow-access]'));
              const luminance=c=>c.match(/[0-9.]+/g).slice(0,3).map(Number).map(v=>v/255)
                .map(v=>v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4)).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
              const a=luminance(style.color),b=luminance(style.backgroundColor);
              return {contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),width:card.clientWidth,scrollWidth:card.scrollWidth};
            })())""")
            result = json.loads(result)
            self.assertGreaterEqual(result["contrast"], 4.5)
            self.assertLessEqual(result["scrollWidth"], result["width"] + 1)

    def test_stale_native_button_cannot_approve_a_new_request(self):
        self.question("file_access")
        self.panel._activate_native()
        card = self.panel.agent_interaction_card
        old = next(b for b in card.findChildren(QPushButton) if b.text() == "允许这次访问")
        updated = deepcopy(self.chat.pending_agent_interaction)
        updated["id"] = "grant-new"
        updated["resource"]["path"] = "C:\\Selected\\different.csv"
        self.chat.pending_agent_interaction = updated
        self.chat.agent_interaction_changed.emit(updated)
        old.click()
        self.assertEqual(self.chat.answers, [])


if __name__ == "__main__":
    unittest.main()
