"""Explicit synthetic WebEngine draft restart/late-commit QA; offline default.

No real profile, credential provider, user files or model/network calls. --run
opens only controlled QA windows; screenshots contain generated draft text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
SOURCES = ("desktop_pet/draft_store.py", "desktop_pet/draft_owner.py", "desktop_pet/ui/draft_state.py",
    "desktop_pet/ui/chat_panel.py", "desktop_pet/ui/assistant_panel.py", "desktop_pet/chat_controller.py",
    "desktop_pet/async_tasks.py", "frontend/chat-ui/src/App.tsx", "frontend/chat-ui/src/bridge.ts",
    "desktop_pet/ui/web_chat/index.html", "tools/qa_local_chat_drafts.py",
    "desktop_pet/conversations/store.py", "desktop_pet/conversations/chronology.py",
    "tests/test_application.py", "tests/test_assistant_ui.py")


def digest(raw): return hashlib.sha256(raw).hexdigest()
def source_hashes(): return {path: digest((ROOT/path).read_bytes()) for path in SOURCES}


def exercise(output):
    from PySide6.QtCore import QEvent, QThreadPool
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from desktop_pet.api_client import ChatReply
    from desktop_pet.chat_controller import ChatController
    from desktop_pet.conversations import ConversationStore
    from desktop_pet.settings_manager import ApiSettings
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
    from tests.test_assistant_ui import set_dom_text, click_dom, wait_js, eval_js, wait_until

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    (ROOT/"agent_workspace").mkdir(exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="qa-draft-rendered-", dir=ROOT/"agent_workspace"))
    archive = ConversationStore(runtime/"archive.sqlite3")
    settings = isolated_settings(runtime/"settings.ini", MemoryCredentialBackend())
    settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "synthetic-no-network", 3))
    settings.save_api_key("SYNTHETIC_DRAFT_RENDERED_PRIVATE_KEY")
    controllers, panels, checks, screenshots = [], [], {}, []
    release = threading.Event()

    class NoNetworkClient:
        def __init__(self): self.calls = 0
        def complete_context(self, *args, **kwargs):
            self.calls += 1
            return ChatReply("受控合成回复，无网络调用。")

    client = NoNetworkClient()

    def new_chat():
        chat = ChatController(settings, client, store=archive, metadata_client_factory=lambda: None)
        controllers.append(chat)
        return chat

    def new_panel(chat):
        panel = AssistantChatPanel(chat)
        panel.setWindowTitle("AURA 本地草稿 QA · 合成数据")
        panel.resize(1040, 790)
        panels.append(panel)
        panel.show()
        wait_until(lambda: panel.web_view is not None and panel._frontend_ready, timeout=18,
                   message="Controlled WebEngine did not finish its own bridge handshake")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=message-input]'))")
        wait_js(panel, "!document.querySelector('[data-testid=message-input]').disabled")
        return panel

    def text(panel): return eval_js(panel, "document.querySelector('[data-testid=message-input]').value")

    def close_chat(chat, panel):
        panel.close()
        chat.shutdown()
        spin_until(lambda: chat._local_draft_owner._stopped.is_set())
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        panel.deleteLater(); app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()

    def shot(panel, name):
        QTest.qWait(100)
        path = output/name
        if not panel.grab().save(str(path), "PNG"): raise RuntimeError("QA_SCREENSHOT_FAILED")
        screenshots.append({"name": name, "sha256": digest(path.read_bytes())})

    try:
        first = new_chat(); original_cid = first.current_conversation_id
        first_panel = new_panel(first)
        original_text = "鲸鱼娘，记住这段尚未发送的草稿。\n明天再继续整理项目。"
        set_dom_text(first_panel, "[data-testid=message-input]", original_text)
        spin_until(lambda: first_panel._draft_owner.snapshot(original_cid).text == original_text)
        if not first_panel._draft_owner.wait(first_panel._draft_owner.flush(), 2.):
            raise RuntimeError("QA_DRAFT_FLUSH_FAILED")
        close_chat(first, first_panel)

        second = new_chat(); second_panel = new_panel(second)
        spin_until(lambda: second.current_conversation_id == original_cid)
        wait_js(second_panel, "document.querySelector('[data-testid=message-input]').value === "+json.dumps(original_text))
        checks["ACTUAL_WEBENGINE_UNARCHIVED_CID_RESTORED"] = second.current_conversation_id == original_cid
        checks["ACTUAL_RENDERED_DRAFT_TEXT_RESTORED"] = text(second_panel) == original_text
        checks["RESTORATION_DID_NOT_ARCHIVE_OR_SEND"] = archive.get_conversation(original_cid) is None and not second.messages and client.calls == 0
        checks["NO_STAGED_IMAGE_REFERENCE_RESTORED"] = not second_panel._staged_images
        shot(second_panel, "restored-local-draft.png")

        # Same visible panel: native fallback and the real WebEngine page use
        # the same owner; the hidden native input may not overwrite the web.
        second_panel._activate_native()
        checks["NATIVE_FALLBACK_RESTORES_SAME_TEXT"] = second_panel.input.toPlainText() == original_text
        native_text = "切换兼容界面后继续输入的草稿。"
        second_panel.input.setPlainText(native_text)
        second_panel._reload_web()
        # Wait for the replacement document's bridge handshake. The old hidden
        # page can briefly display an already-published native draft while its
        # reload is still scheduled; that is not proof the new DOM is ready.
        wait_until(lambda: second_panel._frontend_ready, timeout=18,
                   message="Replacement WebEngine document did not finish its bridge handshake")
        wait_js(second_panel, "document.querySelector('[data-testid=message-input]').value === "+json.dumps(native_text))
        checks["RETURN_TO_WEB_RESTORES_NATIVE_EDIT"] = text(second_panel) == native_text

        # Real controller, first user-message DB write held before its commit.
        # Accept/send clears only that draft. A new draft is entered before the
        # first message acquires its actual generation, then app shutdown waits
        # for the real accepted writer before its final draft identity bind.
        entered = threading.Event()
        original_append = archive.append_message
        def held_append(*args, **kwargs):
            if args[0] == original_cid and args[1] == "user":
                entered.set()
                if not release.wait(8): raise RuntimeError("QA_CONTROLLED_WRITE_NOT_RELEASED")
            return original_append(*args, **kwargs)
        first_send = "这条只进入合成的本机存档，不访问模型网络。"
        next_text = "第一条保存期间立即输入的下一条草稿，更新后也应保留。"
        with patch.object(archive, "append_message", held_append):
            set_dom_text(second_panel, "[data-testid=message-input]", first_send)
            click_dom(second_panel, "[data-testid=send-button]")
            spin_until(entered.is_set)
            spin_until(lambda: second_panel._draft_owner.snapshot(original_cid).text == "")
            set_dom_text(second_panel, "[data-testid=message-input]", next_text)
            spin_until(lambda: second_panel._draft_owner.snapshot(original_cid).text == next_text)
            if not second_panel._draft_owner.wait(second_panel._draft_owner.flush(), 2.):
                raise RuntimeError("QA_PRECOMMIT_DRAFT_FLUSH_FAILED")
            second.shutdown()
            checks["SHUTDOWN_DID_NOT_FORGET_HELD_ARCHIVE_WRITER"] = not second_panel._draft_owner._stopped.is_set()
            release.set()
            spin_until(lambda: second_panel._draft_owner._stopped.is_set())
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        second_panel.close(); second_panel.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()

        third = new_chat()
        if not third.open_conversation(original_cid): raise RuntimeError("QA_ARCHIVED_CID_OPEN_FAILED")
        spin_until(lambda: not third.history_loading)
        third_panel = new_panel(third)
        wait_js(third_panel, "document.querySelector('[data-testid=message-input]').value === "+json.dumps(next_text))
        checks["LATE_FIRST_COMMIT_NEXT_DRAFT_RENDERED_AFTER_RESTART"] = text(third_panel) == next_text
        checks["ARCHIVE_CONTAINS_ONLY_EXPLICIT_FIRST_SEND"] = [row["content"] for row in archive.load_messages(original_cid)] == [first_send]
        checks["NO_MODEL_NETWORK_REQUEST_OR_DRAFT_AUTO_SEND"] = client.calls == 0
        shot(third_panel, "restored-after-late-first-commit.png")
        close_chat(third, third_panel)
        return {"passed": all(checks.values()), "checks": checks, "screenshots": screenshots,
            "draft_text_sha256": digest(next_text.encode("utf-8")), "model_requests": client.calls,
            "synthetic_dataset_retained": True, "dataset_id": runtime.name}
    finally:
        release.set()
        for chat in controllers: chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for chat in controllers:
            owner = getattr(chat, "_local_draft_owner", None)
            if owner is not None: owner.shutdown(1.)
        for panel in panels:
            try: panel.close(); panel.deleteLater()
            except RuntimeError: pass
        for chat in controllers: chat.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT/"artifacts/qa-local-chat-drafts")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    before = source_hashes()
    result = {"passed": False, "status": "not_run"}
    if args.run:
        try: result = {**exercise(args.output), "status": "completed"}
        except Exception as error: result = {"passed": False, "status": "failed", "error_type": type(error).__name__}
    after = source_hashes()
    result.update(schema="aura.local_chat_drafts_rendered_qa.v1", source_before=before, source_after=after,
        source_unchanged=before == after, scope="Synthetic local native/WebEngine UI and SQLite only; no production profile or model request")
    result["passed"] = result["passed"] and before == after
    (args.output/"verification.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "passed", "source_unchanged")}))
    return 0 if result["passed"] or result["status"] == "not_run" else 1


if __name__ == "__main__": raise SystemExit(main())
