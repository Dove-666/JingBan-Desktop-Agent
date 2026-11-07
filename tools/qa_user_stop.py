"""Explicit caller-budgeted Stop journey in the actual shipped chat window.

No credential loading, production profile, hidden reasoning, or raw payload
logging. A synthetic streaming HTTP server can self-test the same journey.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from tools.qa_user_chat import ROOT, fingerprints

_RETAINED = []
LONG_PROMPT = "请从第一句开始写一篇五千字的原创海洋科普长文，介绍海水、海流与海洋生物。直接写正文，不要提纲。"
DRAFT = "这条草稿先不要发送\n停止后还要保留"


def run(configured, output, *, api_count, real_api=True):
    if _RETAINED:
        raise RuntimeError("STOP_QA_PREVIOUS_CLOSURE_UNCONFIRMED")
    url = urlsplit(configured.api.base_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1":
        raise ValueError("STOP_QA_BUDGET_PROXY_REQUIRED")
    from PySide6.QtCore import QEvent, QThreadPool
    from PySide6.QtWidgets import QApplication
    from desktop_pet.api_client import ApiClient
    from desktop_pet.chat_controller import ChatController
    from desktop_pet.conversations import ConversationStore
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    from tests.test_application import MemoryCredentialBackend, isolated_settings
    from tests.test_assistant_ui import click_dom, eval_js, set_dom_text, wait_js, wait_until

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        raise RuntimeError("CREATE_QAPPLICATION_FIRST")
    output = Path(output).resolve()
    if output.exists() or not output.is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("STOP_QA_NEW_REPORT_REQUIRED")
    output.mkdir(parents=True)
    runtime = Path(tempfile.mkdtemp(prefix="user-stop-", dir=ROOT / "agent_workspace"))
    def sources():
        return {**fingerprints(), "tools/qa_user_stop.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    before = sources()
    settings = isolated_settings(runtime / "settings.ini", MemoryCredentialBackend())
    settings.save_api_settings(configured.api); settings.save_api_key(configured.key)
    archive = ConversationStore(runtime / "archive.sqlite3")
    class ObservedClient(ApiClient):
        def __init__(self):
            super().__init__(); self.facts = []
        def request_text(self, api, key, messages, **kwargs):
            self.facts.append({"contains_stopped_prompt": any(message.get("role") == "user"
                and message.get("content") == LONG_PROMPT for message in messages)})
            return super().request_text(api, key, messages, **kwargs)
    client = ObservedClient()
    chat = ChatController(settings, client, store=archive, metadata_client_factory=lambda: None)
    panel = AssistantChatPanel(chat); panel.resize(1040, 800)
    panel.setWindowTitle("AURA 用户体验测试 · 停止与下一条消息")
    report = {"schema":"aura.user_stop.v1", "created_at":datetime.now(timezone.utc).isoformat(),
        "real_model_requested":real_api, "api_before":api_count(), "checks":{}, "owned_profile":str(runtime),
        "production_profile_used":False, "production_app_restarted":False, "hidden_reasoning_saved":False,
        "draft_preservation_scope":"visible composer during Stop; no restart/durable-draft claim"}
    checks = report["checks"]
    def screenshot(name):
        if not panel.grab().save(str(output / name), "PNG"):
            raise RuntimeError("STOP_QA_SCREENSHOT_FAILED")
    try:
        panel.show()
        wait_until(lambda: panel._frontend_ready and not chat.history_loading, timeout=25)
        set_dom_text(panel, "#message-input", LONG_PROMPT)
        wait_js(panel, "!document.querySelector('[data-testid=send-button]').disabled")
        click_dom(panel, "[data-testid=send-button]")
        wait_until(lambda: chat.busy, timeout=8)
        set_dom_text(panel, "#message-input", DRAFT)
        wait_until(lambda: chat.can_stop and chat.streaming_reply and bool(chat.streaming_reply.get("text")), timeout=120)
        checks["prefix_arrived_before_terminal"] = chat.busy
        prefix = chat.streaming_reply["text"]
        screenshot("01-prefix.png")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=stop-button]'))")
        started = time.monotonic()
        click_dom(panel, "[data-testid=stop-button]")
        wait_until(lambda: not chat.busy and not QThreadPool.globalInstance().activeThreadCount(), timeout=12)
        report["stop_settled_seconds"] = round(time.monotonic() - started, 3)
        checks["stop_settled_within_three_seconds"] = report["stop_settled_seconds"] < 3
        stopped = chat.streaming_reply or {}
        checks["partial_retained_as_interrupted"] = stopped.get("state") == "interrupted" and str(stopped.get("text", "")).startswith(prefix)
        checks["new_multiline_draft_preserved"] = eval_js(panel, "document.querySelector('#message-input').value") == DRAFT
        messages = archive.load_messages(chat.current_conversation_id)
        users = [m for m in messages if m.get("role") == "user"]
        checks["one_cancelled_user_message"] = len(users) == 1 and users[0].get("metadata", {}).get("request_status") == "cancelled"
        checks["no_completed_reply_for_stopped_turn"] = not any(m.get("role") == "assistant" for m in messages)
        checks["stop_did_not_retry_api"] = api_count() == report["api_before"] + 1
        screenshot("02-stopped-draft.png")
        set_dom_text(panel, "#message-input", "请只回复：可以继续。")
        wait_js(panel, "!document.querySelector('[data-testid=send-button]').disabled")
        click_dom(panel, "[data-testid=send-button]")
        wait_until(lambda: chat.busy, timeout=8)
        wait_until(lambda: not chat.busy, timeout=120)
        replies = [m for m in chat.messages if m.get("role") == "assistant"]
        answer = str(replies[-1].get("content", "")) if replies else ""
        checks["next_turn_completes_once"] = len(replies) == 1 and re.sub(r"[。.!！～~\s]+", "", answer) == "可以继续"
        saved = archive.load_messages(chat.current_conversation_id)
        saved_users = [message for message in saved if message.get("role") == "user"]
        saved_replies = [message for message in saved if message.get("role") == "assistant"]
        checks["saved_turn_identity_and_status"] = (len(saved_users) == 2 and len(saved_replies) == 1
            and saved_users[0].get("metadata", {}).get("request_status") == "cancelled"
            and saved_users[1].get("metadata", {}).get("request_status") == "complete"
            and saved_replies[0].get("metadata", {}).get("reply_to") == saved_users[1]["id"])
        rendered = ""
        if replies:
            selector = "[data-message-id=" + json.dumps(str(replies[-1]["id"])) + "] .markdown"
            expression = "document.querySelector(" + json.dumps(selector) + ")?.innerText || ''"
            wait_until(lambda: bool(eval_js(panel, expression)), timeout=8)
            rendered = eval_js(panel, expression)
        checks["next_final_reply_rendered"] = re.sub(r"[。.!！～~\s]+", "", rendered) == "可以继续"
        # A stored reply can reach React before the final busy=false snapshot.
        # Wait for the actual controls to leave saving/streaming state before
        # calling the visible journey complete or taking its final screenshot.
        wait_until(lambda: not QThreadPool.globalInstance().activeThreadCount(), timeout=8)
        wait_js(panel, "!document.querySelector('[data-testid=stop-button]') && !document.querySelector('[data-stream-state=streaming]')", timeout=8)
        checks["final_visible_controls_idle"] = True
        checks["next_request_excludes_stopped_prompt"] = len(client.facts) == 2 and client.facts == [
            {"contains_stopped_prompt": True}, {"contains_stopped_prompt": False}]
        checks["next_turn_has_no_error"] = not chat.error
        checks["two_total_counted_requests"] = api_count() == report["api_before"] + 2
        report["visible_final_reply"] = answer[:300]
        screenshot("03-next-complete.png")
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error_code"] = "STOP_QA_JOURNEY_FAILED"
    finally:
        if chat.busy: chat.stop_request()
        panel.close(); chat.shutdown()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (QThreadPool.globalInstance().activeThreadCount()
                or not chat._local_draft_owner._stopped.is_set()):
            app.processEvents(); time.sleep(.01)
        closed = not QThreadPool.globalInstance().activeThreadCount() and chat._local_draft_owner._stopped.is_set()
        checks["exact_local_workers_drained"] = closed
        if closed:
            panel.deleteLater(); app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
        else: _RETAINED.append((chat, panel, archive, settings))
        report["source_before"] = before; report["source_after"] = sources()
        checks["source_unchanged"] = before == report["source_after"]
        report["api_after"] = api_count(); report["transport_entry_facts"] = client.facts
        report["passed"] = not report.get("error_code") and len(checks) == 16 and all(checks.values())
        (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
