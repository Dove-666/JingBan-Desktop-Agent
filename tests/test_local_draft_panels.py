"""Qt text drafts with isolated controllers/storage; no model or user profile."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.draft_store import LocalDraftStore, new_draft
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel
from desktop_pet.ui.draft_state import archive_identity_reader
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until


class LocalDraftPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-local-draft-panels-")
        self.root = Path(self.owned.name)
        self.store = ConversationStore(self.root/"archive.sqlite3")
        self.settings = isolated_settings(self.root/"settings.ini", MemoryCredentialBackend())
        self.controllers, self.panels, self.releases = [], [], []

    def chat(self):
        chat = ChatController(self.settings, store=self.store, metadata_client_factory=lambda: None)
        self.controllers.append(chat)
        return chat

    def panel(self, chat, cls=ChatPanel):
        panel = cls(chat); self.panels.append(panel)
        return panel

    def ready(self, panel):
        spin_until(lambda: panel._draft_owner._loaded.is_set())
        self.app.processEvents()

    def flush(self, panel):
        self.assertTrue(panel._draft_owner.wait(panel._draft_owner.flush(), 2.), panel._draft_owner.last_error)

    def archived(self, cid=None):
        cid = cid or str(uuid4())
        self.store.append_message(cid, "user", "合成已归档内容", "user", message_id=str(uuid4()))
        return cid

    def open(self, chat, cid):
        self.assertTrue(chat.open_conversation(cid))
        spin_until(lambda: not chat.history_loading)

    def tearDown(self):
        for release in self.releases: release.set()
        for chat in self.controllers: chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for chat in self.controllers:
            owner = getattr(chat, "_local_draft_owner", None)
            if owner is not None:
                owner.shutdown(2.)
                self.assertTrue(owner._stopped.is_set())
        for panel in self.panels: panel.deleteLater()
        for chat in self.controllers: chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.owned.cleanup()

    def test_existing_conversation_native_draft_restores_after_controller_restart(self):
        cid = self.archived()
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("已有会话未发送\n  保留空格")
        self.flush(panel); chat.shutdown()
        spin_until(lambda: panel._draft_owner._stopped.is_set())
        restored_chat = self.chat(); self.open(restored_chat, cid)
        restored = self.panel(restored_chat); self.ready(restored)
        self.assertEqual("已有会话未发送\n  保留空格", restored.input.toPlainText())
        self.assertEqual(1, len(self.store.load_messages(cid)))

    def test_background_identity_reader_handles_brand_new_archive_before_other_startup_tasks(self):
        # The draft load may finish before the controller's ordinary initial
        # conversation-list worker has initialized a brand-new archive.
        identity = archive_identity_reader(self.store)(str(uuid4()))
        self.assertFalse(identity.exists)
        self.assertFalse(identity.deleted)
        self.assertEqual("", identity.generation)

    def test_unarchived_draft_reuses_host_validated_original_cid_without_archiving_or_sending(self):
        chat = self.chat(); cid = chat.current_conversation_id
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("首次新会话中尚未发出的草稿")
        self.flush(panel); chat.shutdown()
        spin_until(lambda: panel._draft_owner._stopped.is_set())
        restarted_chat = self.chat(); initially_blank = restarted_chat.current_conversation_id
        self.assertNotEqual(cid, initially_blank)
        restarted = self.panel(restarted_chat)
        spin_until(lambda: restarted_chat.current_conversation_id == cid)
        self.assertEqual("首次新会话中尚未发出的草稿", restarted.input.toPlainText())
        self.assertEqual([], restarted_chat.messages)
        self.assertEqual("新对话", restarted_chat.current_title)
        self.assertIsNone(self.store.get_conversation(cid))

    def test_new_typing_during_background_load_wins_over_unarchived_candidate(self):
        old = str(uuid4())
        side = LocalDraftStore(self.root/"archive.sqlite3.drafts.sqlite3")
        self.assertTrue(side.save(new_draft(old, "先前未发出的本地输入")))
        entered, release = threading.Event(), threading.Event(); self.releases.append(release)
        reader = archive_identity_reader(self.store)
        def held(cid): entered.set(); release.wait(3.); return reader(cid)
        chat = self.chat(); fresh_cid = chat.current_conversation_id
        with patch("desktop_pet.ui.draft_state.archive_identity_reader", return_value=held):
            panel = self.panel(chat)
        self.assertTrue(entered.wait(2.))
        panel.input.setPlainText("用户已经开始新的内容")
        release.set(); self.ready(panel)
        self.assertEqual(fresh_cid, chat.current_conversation_id)
        self.assertEqual("用户已经开始新的内容", panel.input.toPlainText())

    def test_two_panels_share_web_and_native_text_without_hidden_native_overwrite(self):
        cid, other = self.archived(), self.archived()
        chat = self.chat(); self.open(chat, cid)
        native = self.panel(chat); web = self.panel(chat, AssistantChatPanel)
        self.ready(native); self.ready(web)
        self.assertIs(native._draft_owner, web._draft_owner)
        native.input.setPlainText("兼容输入")
        self.assertEqual("兼容输入", web._web_drafts[cid])
        hidden = web.input.toPlainText()
        self.assertTrue(web.dispatch_web_action("draft", {"conversationId": cid, "text": "网页输入更权威"}))
        self.assertEqual(hidden, web.input.toPlainText(), "A hidden native editor should not be laid out per web keystroke")
        self.assertEqual("网页输入更权威", native.input.toPlainText())
        self.open(chat, other); native.input.setPlainText("另一会话草稿")
        self.open(chat, cid)
        self.assertEqual("网页输入更权威", native.input.toPlainText())
        web._activate_native()
        self.assertEqual("网页输入更权威", web.input.toPlainText())
        web.input.setPlainText("切到兼容后的输入")
        self.assertEqual("切到兼容后的输入", native.input.toPlainText())

    def test_generic_agent_context_invalidation_does_not_delete_text_draft(self):
        chat = self.chat(); panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("上下文取消也要保留草稿")
        before = panel._draft_owner.snapshot(chat.current_conversation_id)
        chat.conversation_invalidated.emit(chat.current_conversation_id)
        self.assertEqual(before, panel._draft_owner.snapshot(chat.current_conversation_id))
        self.assertEqual(before.text, panel.input.toPlainText())

    def test_real_clear_and_delete_barriers_prevent_restart_resurrection(self):
        for action in ("clear", "delete"):
            with self.subTest(action=action):
                cid = self.archived()
                chat = self.chat(); self.open(chat, cid)
                panel = self.panel(chat); self.ready(panel)
                panel.input.setPlainText("用户明确移除的草稿"); self.flush(panel)
                if action == "clear":
                    chat.clear(); spin_until(lambda: not chat.history_loading and not self.store.load_messages(cid))
                else:
                    self.assertTrue(chat.delete_conversation(cid)); spin_until(lambda: self.store.get_conversation(cid) is None)
                self.assertEqual("", panel._draft_owner.snapshot(cid).text)
                chat.shutdown(); spin_until(lambda: panel._draft_owner._stopped.is_set())
                side = LocalDraftStore(self.root/"archive.sqlite3.drafts.sqlite3")
                self.assertNotIn(cid, {row.conversation_id for row in side.load()})

    def test_legacy_archive_identity_can_clear_without_being_promoted_to_agent_authority(self):
        cid = "legacy-conversation"
        self.store.append_message(cid, "user", "已有旧版本记录", "user", message_id="legacy-message")
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("旧版本标识的本地输入"); self.flush(panel)
        chat.clear(); spin_until(lambda: not chat.history_loading and not self.store.load_messages(cid))
        self.assertEqual("", panel.input.toPlainText())

    def test_native_accepted_send_cas_keeps_newer_text_and_rejected_send_keeps_original(self):
        chat = self.chat(); panel = self.panel(chat); self.ready(panel)
        cid = chat.current_conversation_id
        panel.input.setPlainText("本次发送的输入")
        with patch.object(chat, "send", return_value=False): panel._send()
        self.assertEqual("本次发送的输入", panel.input.toPlainText())
        def accepted(*args, **kwargs):
            panel._draft_owner.set_text(cid, "提交期间出现的新草稿")
            return True
        with patch.object(chat, "send", side_effect=accepted): panel._send()
        self.assertEqual("提交期间出现的新草稿", panel.input.toPlainText())

    def test_real_first_archive_write_drains_before_shutdown_binds_immediately_typed_next_draft(self):
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "synthetic-draft-only", 3))
        self.settings.save_api_key("SYNTHETIC_LOCAL_DRAFT_NO_NETWORK_KEY")
        chat = self.chat(); cid = chat.current_conversation_id
        panel = self.panel(chat); self.ready(panel)
        entered, release = threading.Event(), threading.Event(); self.releases.append(release)
        original = self.store.append_message
        def held(*args, **kwargs):
            if args[0] == cid and args[1] == "user":
                entered.set()
                if not release.wait(4.): raise RuntimeError("Controlled first write not released")
            return original(*args, **kwargs)
        with patch.object(self.store, "append_message", held), \
             patch.object(chat._client, "complete_context", side_effect=AssertionError("No model call after shutdown")):
            panel.input.setPlainText("首次已接受的输入")
            panel._send(); spin_until(entered.is_set)
            self.assertEqual("", panel.input.toPlainText())
            panel.input.setPlainText("主存档写入前已输入的下一条草稿")
            self.flush(panel)
            side = LocalDraftStore(self.root/"archive.sqlite3.drafts.sqlite3")
            self.assertEqual("", side.lookup(cid).generation)
            chat.shutdown()
            self.assertFalse(panel._draft_owner._stopped.is_set(), "Called shutdown is not a completed archive write")
            release.set()
            spin_until(lambda: panel._draft_owner._stopped.is_set())
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        actual = self.store.load_messages(cid)
        self.assertEqual(["首次已接受的输入"], [row["content"] for row in actual])
        self.assertEqual(actual[0]["id"], side.lookup(cid).generation)
        restarted_chat = self.chat(); self.open(restarted_chat, cid)
        restarted = self.panel(restarted_chat); self.ready(restarted)
        self.assertEqual("主存档写入前已输入的下一条草稿", restarted.input.toPlainText())

    def test_real_archive_clear_failure_restores_draft_without_resurrecting_old_epoch(self):
        cid = self.archived()
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("主存档清空失败应恢复的输入"); self.flush(panel)
        previous = panel._draft_owner.snapshot(cid)
        with patch.object(self.store, "clear_messages", side_effect=OSError("SYNTHETIC_PRIVATE_CLEAR_FAILURE")):
            chat.clear()
            spin_until(lambda: not chat.history_loading)
        self.assertEqual(previous.text, panel.input.toPlainText())
        self.assertNotEqual(previous.epoch, panel._draft_owner.snapshot(cid).epoch)
        self.assertIsNone(panel._draft_owner.set_text(cid, "迟到旧epoch", epoch=previous.epoch))
        self.assertEqual(1, len(self.store.load_messages(cid)))

    def test_archive_clear_error_after_app_shutdown_still_restores_durable_unsent_draft(self):
        cid = self.archived()
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        original_text = "关窗后主存档清空失败，未发草稿仍须保留"
        panel.input.setPlainText(original_text); self.flush(panel)
        entered, release = threading.Event(), threading.Event(); self.releases.append(release)
        def failed_after_shutdown(*args, **kwargs):
            entered.set()
            if not release.wait(4.): raise RuntimeError("Controlled clear failure not released")
            raise OSError("SYNTHETIC_PRIVATE_ARCHIVE_CLEAR_FAILURE")
        with patch.object(self.store, "clear_messages", failed_after_shutdown):
            chat.clear(); spin_until(entered.is_set)
            chat.shutdown()
            release.set()
            spin_until(lambda: panel._draft_owner._stopped.is_set())
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        side = LocalDraftStore(self.root/"archive.sqlite3.drafts.sqlite3")
        self.assertEqual(original_text, side.lookup(cid).text,
                         "Discarded Qt callbacks must not discard draft rollback after the actual clear failed")
        self.assertEqual(1, len(self.store.load_messages(cid)))

    def test_real_archive_commit_then_error_keeps_durable_fence_instead_of_restoring_old_draft(self):
        cid = self.archived()
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("实际清空提交后不可复活的旧输入"); self.flush(panel)
        clear = self.store.clear_messages
        def committed_then_error(*args, **kwargs):
            clear(*args, **kwargs)
            raise OSError("SYNTHETIC_POSTCOMMIT_ACK_FAILURE")
        with patch.object(self.store, "clear_messages", committed_then_error):
            chat.clear(); spin_until(lambda: not chat.history_loading)
        self.assertEqual([], self.store.load_messages(cid))
        self.assertEqual("", panel.input.toPlainText())
        self.assertEqual("DRAFT_ROLLBACK_UNCONFIRMED", panel._draft_owner.last_error)
        self.assertEqual("", LocalDraftStore(self.root/"archive.sqlite3.drafts.sqlite3").lookup(cid).text)

    def test_postclear_readback_error_does_not_undo_confirmed_successful_draft_clear(self):
        cid = self.archived()
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("清空之后的读取错误不能恢复的旧输入"); self.flush(panel)
        read = self.store.get_conversation
        def readback_failed(target):
            if target == cid and not self.store.load_messages(cid):
                raise OSError("SYNTHETIC_POSTCLEAR_READ_FAILURE")
            return read(target)
        with patch.object(self.store, "get_conversation", readback_failed):
            chat.clear(); spin_until(lambda: not chat.history_loading)
        self.assertEqual([], self.store.load_messages(cid))
        self.assertEqual("", panel.input.toPlainText())
        self.flush(panel)
        self.assertEqual("", LocalDraftStore(self.root/"archive.sqlite3.drafts.sqlite3").lookup(cid).text)

    def test_first_host_proactive_archive_message_does_not_lose_a_preexisting_unsent_draft(self):
        chat = self.chat(); cid = chat.current_conversation_id
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("第一条主动评论前就写好的草稿"); self.flush(panel)
        message_id = str(uuid4())
        self.store.append_screen_comment(cid, "合成的首次主动评论", message_id=message_id,
            event_id=str(uuid4()), app_name="Owned draft QA", context_summary="Controlled first host event",
            event_type="scene_change")
        chat.messages_changed.emit()  # Host archive event, no user send or model request.
        self.flush(panel)
        spin_until(lambda: panel._draft_owner.snapshot(cid).generation == message_id)
        chat.shutdown(); spin_until(lambda: panel._draft_owner._stopped.is_set())
        restarted_chat = self.chat(); self.open(restarted_chat, cid)
        restarted = self.panel(restarted_chat); self.ready(restarted)
        self.assertEqual("第一条主动评论前就写好的草稿", restarted.input.toPlainText())

    def test_web_epoch_and_sequence_reject_old_clear_events_and_reordering(self):
        chat = self.chat(); panel = self.panel(chat, AssistantChatPanel); self.ready(panel)
        cid = chat.current_conversation_id
        epoch = panel._draft_owner.snapshot(cid).epoch
        base_version = panel._draft_owner.snapshot(cid).revision
        def dispatch(text, sequence, expected_epoch=epoch):
            return json.loads(panel.bridge.dispatch("draft", json.dumps({"conversationId": cid,
                "text": text, "draftEpoch": expected_epoch, "draftSequence": sequence,
                "draftBaseVersion": base_version, "draftDocument": panel._web_document_token})))["ok"]
        self.assertTrue(dispatch("最新输入", 2))
        self.assertFalse(dispatch("迟到的旧输入", 1))
        self.assertEqual("最新输入", panel._web_drafts[cid])
        ticket = panel._draft_owner.begin_invalidation(cid)
        self.assertTrue(panel._draft_owner.wait_invalidation(ticket, 2.))
        self.assertFalse(dispatch("清空前迟到的输入", 3))
        self.assertEqual("", panel._draft_owner.snapshot(cid).text)
        self.assertFalse(json.loads(panel.bridge.dispatch("draft", json.dumps({"conversationId": cid, "text": "缺少epoch"})))["ok"])

    def test_queued_web_event_from_another_panel_cannot_overwrite_newer_native_edit(self):
        chat = self.chat(); cid = chat.current_conversation_id
        web = self.panel(chat, AssistantChatPanel); native = self.panel(chat)
        self.ready(web); self.ready(native)
        epoch = web._draft_owner.snapshot(cid).epoch
        first = {"conversationId": cid, "draftEpoch": epoch, "draftSequence": 1, "text": "A网页初始输入",
                 "draftBaseVersion": web._draft_owner.snapshot(cid).revision, "draftDocument": web._web_document_token}
        self.assertTrue(json.loads(web.bridge.dispatch("draft", json.dumps(first)))["ok"])
        queued = {**first, "draftSequence": 2, "text": "A网页之前排队的旧全量输入"}
        native.input.setPlainText("B兼容窗口更新的输入")
        self.assertEqual("B兼容窗口更新的输入", web._draft_owner.snapshot(cid).text)
        delivered = json.loads(web.bridge.dispatch("draft", json.dumps(queued)))
        self.assertFalse(delivered["ok"], "A sequence increase is not proof that its base draft is still current")
        self.assertEqual("B兼容窗口更新的输入", native.input.toPlainText())
        state = web.web_state()
        self.assertEqual(2, state["draftSequences"][cid], "A rejected sequence must be acknowledged for convergence")
        fresh = {**queued, "text": "A看过B更新后新的真实输入", "draftSequence": 3,
                 "draftBaseVersion": state["draftVersions"][cid]}
        self.assertTrue(json.loads(web.bridge.dispatch("draft", json.dumps(fresh)))["ok"])
        self.assertEqual(fresh["text"], native.input.toPlainText())

    def test_fast_same_pane_typing_keeps_its_chain_without_per_key_state_publication(self):
        chat = self.chat(); web = self.panel(chat, AssistantChatPanel); self.ready(web)
        cid = chat.current_conversation_id
        start = web._draft_owner.snapshot(cid)
        with patch.object(web, "_queue_web_state") as publish, \
             patch.object(web.input, "setPlainText", wraps=web.input.setPlainText) as hidden:
            for sequence in range(1, 41):
                payload = {"conversationId": cid, "text": "快速连打"+str(sequence), "draftSequence": sequence,
                    "draftEpoch": start.epoch, "draftBaseVersion": start.revision, "draftDocument": web._web_document_token}
                self.assertTrue(json.loads(web.bridge.dispatch("draft", json.dumps(payload)))["ok"])
            self.assertEqual(0, publish.call_count)
            self.assertEqual(0, hidden.call_count)
        self.flush(web)
        self.assertEqual("快速连打40", web._draft_owner.snapshot(cid).text)

    def test_web_send_acceptance_does_not_clear_a_newer_owner_revision(self):
        chat = self.chat(); web = self.panel(chat, AssistantChatPanel); self.ready(web)
        cid = chat.current_conversation_id
        start = web._draft_owner.snapshot(cid)
        payload = {"conversationId": cid, "text": "本次明确发送的输入", "draftSequence": 1,
            "draftEpoch": start.epoch, "draftBaseVersion": start.revision, "draftDocument": web._web_document_token}
        self.assertTrue(json.loads(web.bridge.dispatch("draft", json.dumps(payload)))["ok"])
        def accepted(*args, **kwargs):
            web._draft_owner.set_text(cid, "提交过程中另一个面板的新编辑")
            return True
        with patch.object(chat, "send", side_effect=accepted):
            self.assertTrue(json.loads(web.bridge.dispatch("send", json.dumps(payload)))["ok"])
        self.assertEqual("提交过程中另一个面板的新编辑", web._draft_owner.snapshot(cid).text)
        stale = {**payload, "draftSequence": 2, "text": "之前排队的旧内容"}
        self.assertFalse(json.loads(web.bridge.dispatch("draft", json.dumps(stale)))["ok"])

    def test_document_reload_rejects_old_page_pulse_and_new_page_seeds_existing_sequence(self):
        chat = self.chat(); web = self.panel(chat, AssistantChatPanel); self.ready(web)
        cid = chat.current_conversation_id
        start = web._draft_owner.snapshot(cid)
        payload = {"conversationId": cid, "text": "旧页面已确认输入", "draftSequence": 8,
            "draftEpoch": start.epoch, "draftBaseVersion": start.revision, "draftDocument": web._web_document_token}
        self.assertTrue(json.loads(web.bridge.dispatch("draft", json.dumps(payload)))["ok"])
        old_pulse = {**payload, "draftSequence": 9, "text": "没有新用户操作的旧document脉冲"}
        with patch.object(web, "_ensure_web"): web._reload_web()
        self.assertFalse(json.loads(web.bridge.dispatch("draft", json.dumps(old_pulse)))["ok"])
        self.assertFalse(json.loads(web.bridge.dispatch("ready", json.dumps({"draftDocument": payload["draftDocument"]})))["ok"])
        state = web.web_state()
        self.assertEqual(8, state["draftSequences"][cid])
        next_input = {**old_pulse, "text": "新document中的真实输入",
                      "draftDocument": state["draftDocument"], "draftBaseVersion": state["draftVersions"][cid]}
        self.assertTrue(json.loads(web.bridge.dispatch("draft", json.dumps(next_input)))["ok"])
        self.assertEqual(next_input["text"], web._draft_owner.snapshot(cid).text)

    def test_staged_image_reference_is_not_persisted_or_restored_with_text(self):
        cid = self.archived()
        chat = self.chat(); self.open(chat, cid)
        panel = self.panel(chat); self.ready(panel)
        panel.input.setPlainText("仅文字保留")
        panel._staged_images[cid] = {"image_id": "f"*64, "conversationId": cid}
        self.flush(panel); chat.shutdown(); spin_until(lambda: panel._draft_owner._stopped.is_set())
        restarted_chat = self.chat(); self.open(restarted_chat, cid)
        restarted = self.panel(restarted_chat); self.ready(restarted)
        self.assertEqual("仅文字保留", restarted.input.toPlainText())
        self.assertEqual({}, restarted._staged_images)


if __name__ == "__main__": unittest.main()
