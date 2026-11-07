"""Hidden actual panel/native-dialog boundary; owned files, no model/network."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_session_ui import SessionChat


REFERENCE = "artifact_" + "a" * 64
PRIVATE = "PRIVATE_PATH_EXCEPTION_OR_KEY_CANARY"
FAILED = {"ok": False, "status": "failed", "error": "ARTIFACT_SAVE_FAILED"}


class ArtifactChat(SessionChat):
    def __init__(self):
        super().__init__()
        self.agent_artifacts = [{"reference": REFERENCE, "name": "owned.txt", "mime": "text/plain",
            "bytes": 5, "sha256": hashlib.sha256(b"owned").hexdigest(),
            "verification_scope": "format_and_readback", "private_path": PRIVATE}]
        self.exports = []
        self.result = True
        self.failure = None

    def export_agent_artifact(self, reference, path, *, conversation_id=None):
        self.exports.append((reference, path, conversation_id))
        if self.failure is not None: raise self.failure
        if self.result: Path(path).write_bytes(b"owned")
        return self.result


class ArtifactSaveReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.parent = (Path(__file__).resolve().parents[1] / "agent_workspace").resolve(strict=True)
        self.owner = tempfile.TemporaryDirectory(prefix="artifact-receipt-test-", dir=self.parent)
        self.root = Path(self.owner.name).resolve(strict=True)
        self.assertEqual(self.root.parent, self.parent)
        self.target = self.root / (PRIVATE + ".txt")
        self.chat = ArtifactChat()
        self.panel = AssistantChatPanel(self.chat)
        self.payload = {"conversationId": "current", "reference": REFERENCE}

    def tearDown(self):
        for timer in (self.panel._web_publish_timer, self.panel._web_stream_timer,
                      self.panel._web_start_timer, self.panel._web_timeout): timer.stop()
        self.assertFalse(self.panel.isVisible()); self.assertIsNone(self.panel.web_view)
        self.panel.deleteLater(); self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete); self.app.processEvents()
        self.assertEqual(self.root.resolve(strict=True).parent, self.parent)
        self.owner.cleanup()

    def dispatch(self, payload=None):
        raw = self.panel.bridge.dispatch("openArtifact", json.dumps(self.payload if payload is None else payload))
        self.assertNotIn(PRIVATE, raw)
        self.assertNotIn(str(self.target), raw)
        return json.loads(raw)

    def test_saved_receipt_opens_and_exports_once_and_queues_one_update(self):
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=(str(self.target), "")) as dialog, \
             patch.object(self.panel, "_queue_web_state") as queued:
            self.assertEqual(self.dispatch(), {"ok": True, "status": "saved"})
            dialog.assert_called_once(); queued.assert_called_once()
        self.assertEqual(self.chat.exports, [(REFERENCE, str(self.target), "current")])
        self.assertEqual(self.target.read_bytes(), b"owned")

    def test_cancel_is_not_error_and_never_exports(self):
        self.panel.status_label.setText("owned previous status")
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=("", "")) as dialog:
            self.assertEqual(self.dispatch(), {"ok": False, "status": "cancelled"})
            dialog.assert_called_once()
        self.assertEqual(self.chat.exports, [])
        self.assertEqual(self.panel.status_label.text(), "owned previous status")

    def test_false_export_is_failed_and_not_retried(self):
        self.chat.result = False
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=(str(self.target), "")):
            self.assertEqual(self.dispatch(), FAILED)
        self.assertEqual(len(self.chat.exports), 1)
        self.assertFalse(self.target.exists())

    def test_export_exception_never_leaks_private_message_or_destination(self):
        for kind in (OSError, ValueError, RuntimeError):
            with self.subTest(kind=kind):
                self.chat.exports.clear(); self.chat.failure = kind(PRIVATE)
                with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=(str(self.target), "")):
                    self.assertEqual(self.dispatch(), FAILED)
                self.assertEqual(len(self.chat.exports), 1)
                self.assertNotIn(PRIVATE, self.panel.status_label.text())

    def test_dialog_exception_safely_completes_without_export(self):
        for kind in (OSError, ValueError, RuntimeError):
            with self.subTest(kind=kind), patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", side_effect=kind(PRIVATE)):
                self.assertEqual(self.dispatch(), FAILED)
        self.assertEqual(self.chat.exports, [])

    def test_wrong_session_stale_reference_or_extra_path_never_opens_dialog(self):
        variants = ({"conversationId": "other", "reference": REFERENCE}, {"conversationId": "current"},
                    {"conversationId": "current", "reference": "artifact_" + "b" * 64},
                    {**self.payload, "path": str(self.target)})
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName") as dialog:
            for value in variants: self.assertEqual(self.dispatch(value), FAILED)
            self.chat.history_loading = True
            self.assertEqual(self.dispatch(), FAILED)
            dialog.assert_not_called()
        self.assertEqual(self.chat.exports, [])

    def test_cid_change_inside_modal_cancels_without_export_or_error(self):
        def change(*_):
            self.chat.current_conversation_id = "other"
            return str(self.target), ""
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", side_effect=change) as dialog:
            self.assertEqual(self.dispatch(), {"ok": False, "status": "cancelled"})
            dialog.assert_called_once()
        self.assertEqual(self.chat.exports, []); self.assertFalse(self.target.exists())

    def test_direct_api_stays_boolean_and_does_not_double_export(self):
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=(str(self.target), "")) as dialog:
            self.assertIs(self.panel.dispatch_web_action("openArtifact", self.payload), True)
            dialog.assert_called_once()
        self.assertEqual(len(self.chat.exports), 1)
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=("", "")):
            self.assertIs(self.panel.dispatch_web_action("openArtifact", self.payload), False)
        self.assertEqual(len(self.chat.exports), 1)

    def test_malformed_json_is_structured_failure_and_other_actions_keep_old_shape(self):
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName") as dialog:
            for payload in ("[]", "{", "null"):
                self.assertEqual(json.loads(self.panel.bridge.dispatch("openArtifact", payload)), FAILED)
            dialog.assert_not_called()
        self.assertEqual(json.loads(self.panel.bridge.dispatch("ready", "{}")), {"ok": True})

    def test_actual_controller_export_hash_check_still_rejects_changed_bytes(self):
        self.target.write_bytes(b"previous owned destination")
        self.chat._current_id = "current"
        self.chat.agent_artifact_page = Mock(return_value={**self.chat.agent_artifacts[0],
            "data_base64": base64.b64encode(b"WRONG").decode(), "next_offset": None})
        def actual_export(reference, path, *, conversation_id=None):
            self.chat.exports.append((reference, path, conversation_id))
            return ChatController.export_agent_artifact(self.chat, reference, path, conversation_id=conversation_id)
        self.chat.export_agent_artifact = actual_export
        with patch("desktop_pet.ui.assistant_panel.QFileDialog.getSaveFileName", return_value=(str(self.target), "")):
            self.assertEqual(self.dispatch(), FAILED)
        self.assertEqual(len(self.chat.exports), 1)
        self.assertEqual(self.target.read_bytes(), b"previous owned destination")
        self.assertEqual(list(self.root.glob(".aura-export-*")), [])


if __name__ == "__main__": unittest.main()
