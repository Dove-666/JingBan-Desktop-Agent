"""Native grouping and safe web links use request identity, with owned rows."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.conversations import ConversationStore
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_session_ui import SessionChat


ABSENT = object()
START = "2026-11-06T03:00:00+00:00"


def trace(label, state="completed", started_at=START):
    return {"version": 1, "state": state, "started_at": started_at, "duration_ms": 1000,
        "events": [{"id": label, "kind": "tool", "label": "工具 " + label,
                    "status": "completed", "elapsed_ms": 500}],
        "context": {"model": "owned", "policy": "model", "messages": [
            {"role": "user", "content": "上下文 " + label}]}}


def row(identity, role, execution=None, *, reply_to=ABSENT, source=None, cid="current"):
    metadata = {}
    if execution is not None:
        metadata["execution"] = deepcopy(execution)
    if reply_to is not ABSENT:
        metadata["reply_to"] = reply_to
    return {"id": identity, "conversation_id": cid, "role": role,
            "source": role if source is None else source, "content": "正文 " + identity, "metadata": metadata}


class ExecutionRequestIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = SessionChat()
        self.chat.messages = []
        self.native = ChatPanel(self.chat)
        self.web = AssistantChatPanel(self.chat)
        self.web._web_start_timer.stop()

    def tearDown(self):
        for timer in (self.web._web_start_timer, self.web._web_timeout, self.web._web_publish_timer,
                      self.web._web_stream_timer, self.web._web_execution_timer):
            timer.stop()
        for panel in (self.native, self.web):
            panel.close()
            panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def project(self, rows):
        self.chat.messages = deepcopy(rows)
        self.native._render_messages()
        return self.web.web_state()["messages"]

    def assert_owners(self, identities):
        self.assertEqual(set(self.native._execution_messages), set(identities))

    def test_actual_store_two_pairs_with_same_time_keep_two_processes(self):
        with tempfile.TemporaryDirectory(prefix="request-identity-owned-", dir=Path(__file__).resolve().parents[1] / "agent_workspace") as directory:
            store = ConversationStore(Path(directory) / "history.sqlite3")
            cid, replies = self.chat.current_conversation_id, []
            for label in ("A", "B"):
                user = store.append_message(cid, "user", label, "user", metadata={"execution": trace(label)})
                replies.append(store.complete_request(cid, user["id"], "答复 " + label, execution=trace(label)))
            rows = store.load_messages(cid)
            projected = self.project(rows)
            self.assert_owners([item["id"] for item in replies])
            for item, expected in zip(replies, ("A", "B")):
                self.assertEqual(self.native._execution_messages[item["id"]]["context"]["messages"][0]["content"], "上下文 " + expected)
                self.assertEqual(next(entry for entry in projected if entry["id"] == item["id"])["metadata"]["reply_to"], item["metadata"]["reply_to"])
            self.assertEqual(store.load_messages(cid), rows)

    def test_explicit_queued_links_override_adjacent_position_and_missing_reply_trace(self):
        projected = self.project([row("u1", "user", trace("A")), row("u2", "user", trace("B")),
            row("a1", "assistant", reply_to="u1"), row("a2", "assistant", trace("B"), reply_to="u2")])
        self.assert_owners(["a1", "a2"])
        self.assertEqual(self.native._execution_messages["a1"]["events"][0]["label"], "工具 A")
        self.assertEqual(self.native._execution_messages["a2"]["events"][0]["label"], "工具 B")
        self.assertEqual(projected[2]["metadata"]["reply_to"], "u1")

    def test_null_invalid_nonstring_and_unloaded_links_never_enter_legacy_fallback(self):
        for link in (None, "foreign-user", "", 1, [], "a1"):
            with self.subTest(link=link):
                projected = self.project([row("u1", "user", trace("A")), row("a1", "assistant", reply_to=link)])
                self.assert_owners(["u1"])
                self.assertIsNone(projected[1]["metadata"]["reply_to"])

    def test_genuinely_absent_legacy_link_keeps_ordinary_adjacent_fallback(self):
        projected = self.project([row("u1", "user", trace("A")), row("a1", "assistant")])
        self.assert_owners(["a1"])
        self.assertNotIn("reply_to", projected[1]["metadata"])

    def test_proactive_process_is_independent_and_does_not_consume_legacy_reply(self):
        self.project([row("u1", "user", trace("A")),
            row("p1", "assistant", trace("P"), reply_to="u1", source="proactive_screen"),
            row("a1", "assistant")])
        self.assert_owners(["p1", "a1"])
        self.assertEqual(self.native._execution_messages["p1"]["context"]["messages"][0]["content"], "上下文 P")
        self.assertEqual(self.native._execution_messages["a1"]["context"]["messages"][0]["content"], "上下文 A")

    def test_completed_and_needs_user_user_only_trace_inherit_exact_linked_reply(self):
        for state in ("completed", "needs_user"):
            with self.subTest(state=state):
                self.project([row("u1", "user", trace("A", state)), row("other", "user"),
                    row("a1", "assistant", reply_to="u1")])
                self.assert_owners(["a1"])
                self.assertEqual(self.native._execution_messages["a1"]["state"], state)

    def test_running_failed_interrupted_user_only_trace_stays_on_request(self):
        for state in ("running", "failed", "interrupted"):
            with self.subTest(state=state):
                self.project([row("u1", "user", trace("A", state)), row("a1", "assistant", reply_to="u1")])
                self.assert_owners(["u1"])

    def test_two_attempts_of_one_request_keep_both_contexts(self):
        self.project([row("u1", "user", trace("new", "running", "2026-11-06T03:00:01+00:00")),
            row("a1", "assistant", trace("old"), reply_to="u1")])
        self.assert_owners(["u1", "a1"])
        self.assertEqual(self.native._execution_messages["u1"]["context"]["messages"][0]["content"], "上下文 new")
        self.assertEqual(self.native._execution_messages["a1"]["context"]["messages"][0]["content"], "上下文 old")

    def test_explicit_linked_user_is_not_claimed_again_by_unlinked_assistant(self):
        self.project([row("u1", "user", trace("A")), row("a1", "assistant", trace("A"), reply_to="u1"),
            row("a2", "assistant", trace("separate"))])
        self.assert_owners(["a1", "a2"])

    def test_foreign_conversation_rows_are_removed_before_pairing_and_display(self):
        projected = self.project([row("u1", "user", trace("A")), row("foreign-user", "user", trace("F"), cid="foreign"),
            row("foreign-answer", "assistant", trace("F"), reply_to="u1", cid="foreign"),
            row("a1", "assistant", reply_to="foreign-user")])
        self.assert_owners(["u1"])
        self.assertEqual([item["id"] for item in projected], ["u1", "a1"])
        self.assertNotIn("foreign-user", self.native.transcript.toPlainText())
        self.assertIsNone(projected[-1]["metadata"]["reply_to"])

    def test_non_user_source_cannot_be_a_link_target(self):
        projected = self.project([row("u1", "user", trace("A"), source="foreign"),
            row("a1", "assistant", reply_to="u1")])
        self.assert_owners(["u1"])
        self.assertIsNone(projected[1]["metadata"]["reply_to"])

    def test_missing_conversation_id_legacy_rows_keep_the_current_view_contract(self):
        rows = [row("old-u", "user", trace("A")), row("old-a", "assistant", trace("A"), reply_to="old-u")]
        for item in rows:
            item.pop("conversation_id")
        projected = self.project(rows)
        self.assert_owners(["old-a"])
        self.assertEqual(projected[1]["metadata"]["reply_to"], "old-u")


if __name__ == "__main__":
    unittest.main()
