"""Operation-local view redaction with owned settings and hidden Qt widgets."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.settings_manager import SettingsManager
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_chat_stream_bridge import StreamChat


FIRST = "OWNED_FIRST_TEST_VALUE"
NEXT = "OWNED_NEXT_TEST_VALUE"
GUI = "OWNED_GUI_TEST_VALUE"


class OwnedSettings:
    credentials_available = True

    def __init__(self):
        self.values = {"chat": FIRST, "gui": GUI}
        self.samples = 0
        self.reads = []
        self.fail = False
        self.on_sample = None

    def get_known_api_keys(self):
        self.samples += 1
        if self.on_sample is not None:
            self.on_sample()
        if self.fail:
            raise RuntimeError("OWNED_SETTINGS_UNAVAILABLE")
        return SettingsManager.get_known_api_keys(self)

    def get_api_key(self):
        self.reads.append("chat")
        return self.values["chat"]

    def get_agent_gui_api_key(self):
        self.reads.append("gui")
        return self.values["gui"]

    def reset(self):
        self.samples = 0
        self.reads.clear()


class ViewChat(StreamChat):
    _redact_known_secrets = ChatController._redact_known_secrets

    def __init__(self, settings):
        super().__init__()
        self._settings_manager = settings

    def _view_redactor(self):
        return ChatController._view_redactor(self)


class ViewRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.settings = OwnedSettings()
        self.chat = ViewChat(self.settings)
        self.chat.messages = self.messages()
        self.panels = []
        self.panel = self.keep(AssistantChatPanel(self.chat))
        self.settings.reset()

    def tearDown(self):
        for panel in self.panels:
            for name in ("_web_publish_timer", "_web_stream_timer", "_web_execution_timer",
                         "_web_start_timer", "_web_timeout"):
                timer = getattr(panel, name, None)
                if timer is not None:
                    timer.stop()
            self.assertFalse(panel.isVisible())
            if isinstance(panel, AssistantChatPanel):
                self.assertIsNone(panel.web_view)
            panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def keep(self, panel):
        self.panels.append(panel)
        return panel

    def messages(self, count=20):
        trace = {"version": 1, "state": "completed", "started_at": "2026-11-06T00:00:00+00:00",
            "duration_ms": 1000, "events": [{"id": "summary:completed", "kind": "summary",
                "label": "回复已完成", "status": "completed", "elapsed_ms": 1000}],
            "context": {"model": "owned-model", "policy": "model", "messages": [
                {"role": "user", "content": "owned context " + "字" * 200} for _ in range(40)]}}
        trace["context"]["messages"][0]["content"] = f"{FIRST} {NEXT} {GUI}"
        return [{"id": f"owned-{index}", "conversation_id": self.chat.current_conversation_id,
            "role": "user" if index % 2 == 0 else "assistant",
            "source": "user" if index % 2 == 0 else "assistant", "content": "owned message",
            "metadata": {"request_status": "complete", "execution": deepcopy(trace)}} for index in range(count)]

    def assert_one_sample(self):
        self.assertEqual(self.settings.samples, 1)
        self.assertEqual(self.settings.reads, ["chat", "gui"])

    def test_web_full_samples_once_and_refreshes_on_the_next_operation(self):
        state = self.panel.web_state()
        self.assert_one_sample()
        self.assertEqual(len(state["messages"]), 20)
        for row in state["messages"]:
            self.assertNotIn(FIRST, json.dumps(row["metadata"]["execution"]))
            self.assertNotIn(GUI, json.dumps(row["metadata"]["execution"]))
        self.settings.values["chat"] = NEXT
        self.settings.reset()
        state = self.panel.web_state()
        self.assert_one_sample()
        self.assertTrue(all(NEXT not in json.dumps(row["metadata"]["execution"]) for row in state["messages"]))

    def test_native_full_samples_once_and_refreshes_on_the_next_operation(self):
        native = self.keep(ChatPanel(self.chat))
        self.settings.reset()
        native._render_messages()
        self.assert_one_sample()
        self.assertNotIn(FIRST, json.dumps(native._execution_messages))
        self.settings.values["chat"] = NEXT
        self.settings.reset()
        native._render_messages()
        self.assert_one_sample()
        self.assertNotIn(NEXT, json.dumps(native._execution_messages))

    def test_empty_and_invalid_traces_read_no_credentials_in_both_views(self):
        native = self.keep(ChatPanel(self.chat))
        for rows in ([], [{"id": "invalid", "role": "user", "source": "user", "content": "owned",
                "metadata": {"execution": {"version": 7, "state": "completed"}}}]):
            with self.subTest(empty=not rows):
                self.chat.messages = rows
                self.settings.reset()
                self.panel.web_state()
                native._render_messages()
                self.assertEqual(self.settings.samples, 0)
                self.assertEqual(self.settings.reads, [])

    def test_failed_sample_rejects_traces_without_retrying_and_the_next_view_recovers(self):
        native = self.keep(ChatPanel(self.chat))
        self.settings.fail = True
        self.settings.reset()
        state = self.panel.web_state()
        self.assertEqual(self.settings.samples, 1)
        self.assertTrue(all("execution" not in row["metadata"] for row in state["messages"]))
        self.settings.reset()
        native._render_messages()
        self.assertEqual(self.settings.samples, 1)
        self.assertEqual(native._execution_messages, {})
        self.settings.fail = False
        self.settings.reset()
        self.assertTrue(all("execution" in row["metadata"] for row in self.panel.web_state()["messages"]))
        self.assert_one_sample()

    def test_lazy_sample_is_reentrant_safe_and_later_use_does_not_retry_failure(self):
        holder = SimpleNamespace(_settings_manager=self.settings)
        redact = ChatController._view_redactor(holder)
        self.assertEqual(self.settings.samples, 0)
        nested = []

        def observe():
            with self.assertRaises(ValueError):
                redact(FIRST)
            nested.append(True)

        self.settings.on_sample = observe
        self.assertNotIn(FIRST, redact(FIRST))
        self.assertEqual(nested, [True])
        self.assert_one_sample()
        self.settings.on_sample = None
        self.settings.fail = True
        self.settings.reset()
        failed = ChatController._view_redactor(holder)
        for _ in range(3):
            with self.assertRaises((RuntimeError, ValueError)):
                failed(FIRST)
        self.assertEqual(self.settings.samples, 1)
        self.assertEqual(self.settings.reads, [])

    def test_legacy_fake_redactors_and_missing_redactors_keep_their_projection_contract(self):
        self.chat._view_redactor = None
        self.chat.messages = self.messages(1)
        self.settings.reset()
        state = self.panel.web_state()
        self.assert_one_sample()
        self.assertNotIn(FIRST, json.dumps(state["messages"][0]["metadata"]["execution"]))
        self.chat._redact_known_secrets = None
        self.settings.reset()
        self.assertIn("execution", self.panel.web_state()["messages"][0]["metadata"])
        self.assertEqual(self.settings.samples, 0)

    def test_view_dto_structural_identifiers_do_not_enter_the_redactor(self):
        document = self.panel._web_document_token
        cid = self.chat.current_conversation_id
        self.settings.values = {"chat": document, "gui": cid}
        rows = self.messages(1)
        rows[0]["id"] = document
        rows[0]["metadata"]["execution"]["context"]["messages"][0]["content"] = f"{document} {cid}"
        self.chat.messages = rows
        state = self.panel.web_state()
        self.assertEqual(state["conversationId"].encode(), cid.encode())
        self.assertEqual(state["draftDocument"].encode(), document.encode())
        self.assertEqual(state["messages"][0]["id"].encode(), document.encode())
        self.assertNotIn(document, json.dumps(state["messages"][0]["metadata"]["execution"]))

    def test_twenty_negotiated_stream_updates_read_no_history_or_credentials(self):
        self.panel._read_web_state()
        ready = self.panel.bridge.dispatch("ready", json.dumps({
            "draftDocument": self.panel._web_document_token, "streamProtocol": 1}))
        self.assertTrue(json.loads(ready)["ok"])
        self.chat.busy = True
        self.chat.streaming_reply = {"conversationId": self.chat.current_conversation_id,
            "requestId": "owned-stream", "text": "first", "state": "streaming"}
        self.panel._read_web_state()
        self.settings.reset()
        reads_before = self.chat.history_reads
        patches = []
        self.panel.bridge.streamingChanged.connect(patches.append)
        for index in range(20):
            self.chat.streaming_reply["text"] = "owned " + "字" * (index + 1)
            self.chat.streaming_reply_changed.emit()
            self.panel._web_stream_timer.stop()
            self.panel._publish_web_stream()
        self.assertEqual(len(patches), 20)
        self.assertEqual(self.chat.history_reads, reads_before)
        self.assertEqual(self.settings.samples, 0)
        self.assertEqual(self.settings.reads, [])

    def test_existing_simple_namespace_redaction_caller_remains_supported(self):
        holder = SimpleNamespace(_settings_manager=self.settings)
        value = ChatController._redact_known_secrets(holder, f"{FIRST} {GUI}")
        self.assertNotIn(FIRST, value)
        self.assertNotIn(GUI, value)
        self.assert_one_sample()


if __name__ == "__main__":
    unittest.main()
