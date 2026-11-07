"""User-style chat checks through the shipped Qt page and real controller.

The caller supplies only its budget-proxy API and token. This module never
loads a credential or production profile. All visible text/images are owned
fixtures; reports contain visible replies, never hidden reasoning/API bodies.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_RETAINED = []
SOURCES = ("tools/qa_user_chat.py", "desktop_pet/api_client.py", "desktop_pet/chat_controller.py",
    "desktop_pet/async_tasks.py", "desktop_pet/settings_manager.py", "desktop_pet/chat_images.py",
    "desktop_pet/conversations/store.py", "desktop_pet/draft_store.py", "desktop_pet/draft_owner.py",
    "desktop_pet/ui/chat_panel.py", "desktop_pet/ui/assistant_panel.py", "desktop_pet/ui/draft_state.py",
    "desktop_pet/ui/web_chat/index.html", "frontend/chat-ui/src/App.tsx",
    "frontend/chat-ui/src/bridge.ts", "frontend/chat-ui/src/styles.css",
    "tests/test_assistant_ui.py", "tests/test_application.py")


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def run_first_batch(configured, output, *, api_count, include_images=True, real_api=True, scenario="initial",
                    metadata_topic_terms=("读书", "笔记", "小沐")):
    """Actual UI turns; caller enforces the shared durable API quota."""
    if scenario not in {"initial", "comparison", "metadata"} or scenario == "comparison" and not include_images or scenario == "metadata" and include_images:
        raise ValueError("USER_CHAT_SCENARIO_INVALID")
    if _RETAINED:
        raise RuntimeError("USER_CHAT_PREVIOUS_CLOSURE_UNCONFIRMED")
    endpoint = urlsplit(configured.api.base_url)
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1":
        raise ValueError("USER_CHAT_REQUIRES_BUDGET_PROXY")
    from PySide6.QtCore import QEvent, QThreadPool
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from desktop_pet.api_client import ApiClient
    from desktop_pet.chat_controller import ChatController
    from desktop_pet.conversations import ConversationStore
    from desktop_pet.remote.attachments import ImageInbox
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    from tests.test_application import MemoryCredentialBackend, isolated_settings
    from tests.test_assistant_ui import click_dom, eval_js, set_dom_text, wait_js, wait_until

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        raise RuntimeError("CREATE_QAPPLICATION_BEFORE_CONFIGURED_LOADER")
    app.setQuitOnLastWindowClosed(False)
    output = Path(output).resolve()
    if output.exists() or not output.is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("USE_NEW_OWNED_USER_CHAT_REPORT")
    output.mkdir(parents=True)
    parent = ROOT / "agent_workspace"; parent.mkdir(exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="user-chat-", dir=parent))
    before = fingerprints()
    settings = isolated_settings(runtime / "settings.ini", MemoryCredentialBackend())
    settings.save_api_settings(configured.api)
    settings.save_api_key(configured.key)
    archive = ConversationStore(runtime / "archive.sqlite3")
    class ObservedApiClient(ApiClient):
        # Only a current-user image boolean leaves the actual request boundary.
        # Historical image context can remain in older messages; it must never
        # silently attach an old image to a new user message.
        def __init__(self):
            super().__init__()
            self.wire_facts = []
            self.owned_image_tags = {}

        def request_text(self, api, key, messages, **kwargs):
            current = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), None)
            image = (isinstance(current, list) and any(isinstance(item, dict)
                and item.get("type") == "image_url" for item in current))
            counts = [sum(isinstance(item, dict) and item.get("type") == "image_url"
                for item in m["content"]) if isinstance(m.get("content"), list) else 0
                for m in messages if m.get("role") == "user"]
            tags = [[self.owned_image_tags.get(hashlib.sha256(
                str(item.get("image_url", {}).get("url", "")).encode("utf-8")).hexdigest(), "UNEXPECTED")
                for item in m["content"] if isinstance(item, dict) and item.get("type") == "image_url"]
                if isinstance(m.get("content"), list) else [] for m in messages if m.get("role") == "user"]
            self.wire_facts.append({"current_user_has_image": bool(image), "user_image_counts": counts,
                "owned_image_tags_by_user": tags})
            return super().request_text(api, key, messages, **kwargs)

    client = ObservedApiClient()
    chat = ChatController(settings, client, store=archive,
        metadata_client_factory=None if scenario == "metadata" else lambda: None)
    chat.set_image_inbox(ImageInbox(runtime / "inbox"))
    panel = AssistantChatPanel(chat)
    panel.setWindowTitle("AURA 用户体验测试 · 合成对话")
    panel.resize(1040, 800)
    result = {"schema": "aura.user_chat.v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "budgeted_configured_real_api" if real_api else "synthetic_loopback_harness_selftest",
        "real_api_requested": real_api, "owned_profile": str(runtime), "cases": [],
        "production_profile_read": False, "production_app_restarted": False,
        "hidden_reasoning_or_api_bodies_saved": False, "api_count_before": api_count()}
    result["scenario"] = scenario
    result["image_observation_scope"] = "ApiClient.request_text transport entry; counts and in-memory identity tags, not provider-side capture"

    def save():
        (output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def turn(identifier, prompt, expected, *, draft_during_reply="", image_selected=False,
             expected_image_counts=None, expected_image_tags=None):
        users_before = len([m for m in chat.messages if m.get("role") == "user"])
        assistants_before = len([m for m in chat.messages if m.get("role") == "assistant"])
        count_before = api_count(); wire_before = len(client.wire_facts)
        started = time.monotonic(); partial = None
        set_dom_text(panel, "#message-input", prompt)
        wait_js(panel, "!document.querySelector('[data-testid=send-button]').disabled")
        click_dom(panel, "[data-testid=send-button]")
        wait_until(lambda: chat.busy or len([m for m in chat.messages if m.get("role") == "user"]) > users_before,
            timeout=8, message="The visible Send action was not admitted")
        if draft_during_reply:
            set_dom_text(panel, "#message-input", draft_during_reply)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            preview = chat.streaming_reply
            if preview and preview.get("text") and partial is None:
                partial = round(time.monotonic() - started, 3)
            users = [m for m in chat.messages if m.get("role") == "user"]
            replies = [m for m in chat.messages if m.get("role") == "assistant"]
            if not chat.busy and len(users) > users_before:
                break
            QTest.qWait(10); time.sleep(.002)
        else:
            chat.stop_request()
            raise RuntimeError("USER_CHAT_TURN_DID_NOT_SETTLE")
        replies = [m for m in chat.messages if m.get("role") == "assistant"]
        users = [m for m in chat.messages if m.get("role") == "user"]
        reply = str(replies[-1].get("content", "")) if len(replies) > assistants_before else ""
        rendered = ""
        # Read the final message's rendered text: Markdown source need not be
        # byte-identical to the visible DOM (for example **小沐**).
        if reply:
            selector = "[data-message-id=" + json.dumps(str(replies[-1]["id"])) + "] .markdown"
            expression = "document.querySelector(" + json.dumps(selector) + ")?.innerText || ''"
            wait_until(lambda: bool(eval_js(panel, expression)), timeout=8)
            rendered = eval_js(panel, expression)
        draft = eval_js(panel, "document.querySelector('#message-input').value")
        if scenario == "metadata":
            wait_until(lambda: not chat._metadata_active, timeout=60, message="Metadata did not settle")
        delta = api_count() - count_before
        facts = client.wire_facts[wire_before:]
        checks = {"one_user_message": len(users) == users_before + 1,
            "one_assistant_reply": len(replies) == assistants_before + 1,
            "visible_reply": bool(reply) and bool(rendered),
            "expected_visible_answer": bool(expected(rendered)), "no_public_error": not chat.error,
            "draft_preserved": draft == draft_during_reply,
            "expected_counted_api_requests": delta == (2 if scenario == "metadata" and identifier == "context_intro" else 1),
            "actual_wire_image_matches_current_selection": bool(facts) and all(
                fact["current_user_has_image"] == image_selected for fact in facts),
            "current_user_has_image": bool(users[-1].get("metadata", {}).get("image_id"))}
        if not image_selected:
            checks["current_user_has_no_old_image"] = not checks.pop("current_user_has_image")
        if expected_image_counts is not None:
            checks["actual_wire_image_counts"] = bool(facts) and all(
                fact["user_image_counts"] == expected_image_counts for fact in facts)
        if expected_image_tags is not None:
            checks["transport_entry_owned_image_identity_and_order"] = bool(facts) and all(
                fact["owned_image_tags_by_user"] == expected_image_tags for fact in facts)
        image = output / (identifier + ".png")
        if not panel.grab().save(str(image), "PNG"):
            raise RuntimeError("USER_CHAT_SCREENSHOT_FAILED")
        row = {"id": identifier, "passed": all(checks.values()), "checks": checks,
            "duration_seconds": round(time.monotonic() - started, 3), "first_visible_stream_seconds": partial,
            "api_requests": delta, "request_facts": facts, "visible_reply": reply[:2000],
            "rendered_reply": rendered[:2000],
            "public_error": chat.error, "screenshot": image.name}
        result["cases"].append(row); save()
        set_dom_text(panel, "#message-input", "")
        return row

    try:
        panel.show()
        wait_until(lambda: panel.web_view is not None and panel._frontend_ready and not chat.history_loading,
            timeout=25, message="The shipped chat page did not become ready")
        wait_js(panel, "!document.querySelector('#message-input').disabled")
        if scenario in {"initial", "metadata"}:
            turn("context_intro", "我叫小沐，今天在整理读书笔记。后续回答简短一点，先回我“记住啦”。",
            lambda reply: "记住啦" in reply and len(reply.strip()) <= 80
                and not any(term in reply for term in ("无法记住", "不能记住", "不记得", "没法记住")),
            draft_during_reply="这段下一条草稿还没有发送")
            turn("context_followup", "刚才我说我叫什么？只答称呼。",
            lambda reply: re.sub(r"[。.!！～~\s]+", "", reply) == "小沐")
            if scenario == "metadata":
                saved_conversation = archive.get_conversation(chat.current_conversation_id)
                result["metadata_checks"] = {
                    "generated_title_saved": bool(saved_conversation.get("title_generated")),
                    "concise_topic_title": 2 <= len(chat.current_title) <= 24
                        and any(word in chat.current_title for word in metadata_topic_terms),
                    "summary_saved": bool(chat.current_summary),
                    "one_auxiliary_request_only": api_count() - result["api_count_before"] == 3}
        if include_images:
            from PIL import Image, ImageDraw
            picture = runtime / "shapes.png"
            canvas = Image.new("RGB", (500, 280), "white"); draw = ImageDraw.Draw(canvas)
            draw.ellipse((35, 50, 175, 190), fill="#1768e8")
            draw.ellipse((300, 50, 440, 190), fill="#1768e8")
            draw.rectangle((208, 80, 258, 140), fill="#168c3c")
            canvas.save(picture)
            count_before = api_count()
            if not panel._stage_local_image(str(picture), chat.current_conversation_id):
                raise RuntimeError("USER_CHAT_IMAGE_PICKER_NOT_ADMITTED")
            wait_until(lambda: not panel._image_preparing, timeout=12)
            if not panel._current_staged_image() or panel._image_stage_error:
                raise RuntimeError("USER_CHAT_IMAGE_STAGING_FAILED")
            first_url = chat._load_chat_image(panel._current_staged_image()["image_id"])
            client.owned_image_tags[hashlib.sha256(first_url.encode("utf-8")).hexdigest()] = "A"
            result["select_image_does_not_call_api"] = api_count() == count_before
            wait_js(panel, "Boolean(document.querySelector('.composer-attachment img')?.naturalWidth > 0)")
            turn("image_count", "图片里有几个蓝色圆形？请只回答“X个蓝色圆形”。",
                lambda reply: bool(re.fullmatch(r"(?:2|两|二)个蓝色圆形[。.!！～~]?", re.sub(r"\s+", "", reply))),
                image_selected=True, expected_image_counts=[0, 0, 1] if scenario == "initial" else [1],
                expected_image_tags=[[], [], ["A"]] if scenario == "initial" else [["A"]])
            if scenario == "initial":
                turn("new_topic_without_old_image", "现在换个话题，给我一句不超过二十字的晚安祝福。",
                lambda reply: bool(reply.strip()) and len(reply.strip()) <= 20
                    and any(term in reply for term in ("晚安", "好梦", "美梦"))
                    and not any(term in reply for term in ("蓝色", "圆形")))
            else:
                picture2 = runtime / "changed-shapes.png"
                canvas2 = Image.new("RGB", (500, 280), "white"); draw2 = ImageDraw.Draw(canvas2)
                draw2.ellipse((35, 50, 175, 190), fill="#1768e8")
                draw2.rectangle((208, 80, 258, 140), fill="#168c3c")
                canvas2.save(picture2)
                count_before_second_selection = api_count()
                if not panel._stage_local_image(str(picture2), chat.current_conversation_id):
                    raise RuntimeError("USER_CHAT_SECOND_IMAGE_NOT_ADMITTED")
                wait_until(lambda: not panel._image_preparing, timeout=12)
                if not panel._current_staged_image() or panel._image_stage_error:
                    raise RuntimeError("USER_CHAT_SECOND_IMAGE_STAGING_FAILED")
                second_url = chat._load_chat_image(panel._current_staged_image()["image_id"])
                second_hash = hashlib.sha256(second_url.encode("utf-8")).hexdigest()
                if second_hash in client.owned_image_tags:
                    raise RuntimeError("USER_CHAT_DISTINCT_IMAGES_REQUIRED")
                client.owned_image_tags[second_hash] = "B"
                result["second_image_selection_does_not_call_api"] = api_count() == count_before_second_selection
                wait_js(panel, "Boolean(document.querySelector('.composer-attachment img')?.naturalWidth > 0)")
                turn("two_image_comparison", "这张和上一张相比，蓝色圆形数量有什么变化？只回答多了或少了几个蓝色圆形。",
                    lambda reply: bool(re.fullmatch(r"少了(?:1|一)个蓝色圆形[。.!！～~]?", re.sub(r"\s+", "", reply))),
                    image_selected=True, expected_image_counts=[1, 1], expected_image_tags=[["A"], ["B"]])
                turn("general_image_knowledge", "换个话题，图片压缩算法一般怎么减小文件？用一句话说明。",
                    lambda reply: 5 <= len(reply.strip()) <= 220
                        and any(term in reply for term in ("压缩", "冗余", "量化", "编码")),
                    expected_image_counts=[0, 0, 0], expected_image_tags=[[], [], []])
    except Exception as error:
        result["harness_error_type"] = type(error).__name__
        result["harness_error_code"] = str(error) if re.fullmatch(r"[A-Z][A-Z_]{1,100}", str(error)) else "USER_CHAT_CHECK_FAILED"
    finally:
        if chat.busy:
            chat.stop_request()
        panel.close(); chat.shutdown()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (QThreadPool.globalInstance().activeThreadCount()
                or not chat._local_draft_owner._stopped.is_set()):
            app.processEvents(); time.sleep(.01)
        closed = not QThreadPool.globalInstance().activeThreadCount() and chat._local_draft_owner._stopped.is_set()
        result["exact_local_workers_drained"] = closed
        if closed:
            panel.deleteLater(); app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
        else:
            _RETAINED.append((chat, panel, archive, settings))
        result["api_count_after"] = api_count()
        result["api_count_reconciled"] = result["api_count_after"] - result["api_count_before"] == sum(
            row["api_requests"] for row in result["cases"])
        result["source_before"] = before; result["source_after"] = fingerprints()
        result["source_unchanged"] = result["source_before"] == result["source_after"]
        expected_turns = 3 if scenario == "comparison" else 4 if include_images else 2
        result["passed"] = (len(result["cases"]) == expected_turns
            and all(row["passed"] for row in result["cases"]) and closed and result["source_unchanged"]
            and not result.get("harness_error_type") and result.get("select_image_does_not_call_api", True)
            and result.get("second_image_selection_does_not_call_api", True) and result["api_count_reconciled"]
            and all(result.get("metadata_checks", {"not_requested": True}).values()))
        save()
    return result


if __name__ == "__main__":
    print(json.dumps({"status": "not_run", "real_api_calls": 0,
        "requires": "Caller-owned durable 100-request budget proxy and QApplication"}))
