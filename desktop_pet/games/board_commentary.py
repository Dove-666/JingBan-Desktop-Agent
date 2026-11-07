"""Independent, cancellable dialogue and candidate selection for local board games."""
from __future__ import annotations

import json
from dataclasses import replace
import re
import threading

from PySide6.QtCore import QObject
from ..api_client import ApiClient, ChatReply, redact_secret
from ..async_tasks import TaskRunner
from ..character_cards import CharacterCard
from ..character_prompts import CHARACTER_EXPRESSION_POLICY, character_identity_prompt
from ..emotion_policy import normalize_emotion

def _character_prompt(prompt, card):
    if card is None:
        return prompt
    return (character_identity_prompt(card) + CHARACTER_EXPRESSION_POLICY
            + prompt.replace("鲸鱼娘", "当前角色"))


def _recent_dialogue(state):
    return [{"role": row["role"], "content": row["text"][:1500]}
            for row in state.get("commentary", [])[-8:]
            if isinstance(row, dict) and row.get("role") in {"user", "assistant"}
            and isinstance(row.get("text"), str)]


def _json_response(raw):
    if not isinstance(raw, str) or len(raw) > 16000:
        return None
    raw = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
    try:
        value = json.loads(fenced.group(1) if fenced else raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("reply", ""), str):
        return None
    return value


def _fresh_reaction(reply, recent_dialogue):
    reply = reply.strip()[:240]
    canonical = lambda text: re.sub(r"[\s~～，。！？!?,.、…]+", "", text)
    key = canonical(reply)
    if not key or any(row["role"] == "assistant" and canonical(row["content"]) == key
                      for row in recent_dialogue):
        return ""
    return reply


class BoardGameCommentary(QObject):
    def __init__(self, settings, controller, *, context, identity, game_prompt, move_prompt,
                 api_client=None, move_client=None, runner_factory=TaskRunner, parent=None):
        super().__init__(parent)
        self.settings, self.controller = settings, controller
        self._context, self._identity = context, identity
        self._game_prompt, self._move_prompt = game_prompt, move_prompt
        self.client = api_client if api_client is not None else ApiClient()
        self.move_client = move_client if move_client is not None else ApiClient()
        self.runner = runner_factory(self)
        self._closed = False
        self._active = None
        self._queued = None
        self._move_lock = threading.Lock()
        self._move_identity = self._identity(controller.snapshot())
        self._move_requests = []
        controller.commentary_requested.connect(self.request)
        controller.changed.connect(self._state_changed)

    def _character_snapshot(self):
        loader = getattr(self.settings, "load_character_card", None)
        card = loader() if callable(loader) else None
        if card is not None and not isinstance(card, CharacterCard):
            raise ValueError("Invalid character configuration")
        return card

    def move_selector(self, state):
        """Capture configuration on the GUI thread; only transport runs later."""
        if self._closed:
            return None
        try:
            settings = self.settings.load_api_settings()
            key = self.settings.get_api_key()
            settings.validate(require_model=True)
            if not key:
                return None
            character_card = self._character_snapshot()
        except Exception:
            return None
        settings = replace(settings, timeout_seconds=min(settings.timeout_seconds, 12))
        revision = self._identity(state)
        context = self._context(state)
        recent_dialogue = _recent_dialogue(state)
        client = self.move_client
        prompt = _character_prompt(self._move_prompt, character_card)

        def select(candidates, *, cancellation_event):
            if not candidates:
                return None
            request = (revision, cancellation_event)
            with self._move_lock:
                if self._closed or cancellation_event.is_set() or revision != self._move_identity:
                    return None
                self._move_requests.append(request)
            try:
                choices = {row.get("move") for row in candidates if isinstance(row, dict)}
                messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": redact_secret(json.dumps({"position": context,
                        "recent_dialogue": recent_dialogue, "candidates": candidates}, ensure_ascii=False), key)}]
                raw = client.request_text(settings, key, messages, output_tokens=700,
                                          cancellation_event=cancellation_event).strip()
                with self._move_lock:
                    if self._closed or cancellation_event.is_set() or revision != self._move_identity:
                        return None
                value = _json_response(raw)
                if value is None or value.get("move") not in choices:
                    return None
                reply = redact_secret(value.get("reply", ""), key)
                return {"move": value["move"], "reply": _fresh_reaction(reply, recent_dialogue),
                        "emotion": normalize_emotion(value.get("emotion"))}
            except Exception:
                return None
            finally:
                with self._move_lock:
                    if request in self._move_requests:
                        self._move_requests.remove(request)

        return select

    def _busy(self, value):
        setter = getattr(self.controller, "set_commentary_busy", None)
        if callable(setter):
            setter(value)

    def _state_changed(self, state):
        revision = self._identity(state)
        with self._move_lock:
            self._move_identity = revision
            stale = [event for identity, event in self._move_requests if identity != revision]
            self._move_requests = [request for request in self._move_requests if request[0] == revision]
            for event in stale:
                event.set()
        self._cancel_move_requests(stale)
        if self._active and self._identity(state) != self._active[0]:
            event = self._active[1]
            event.set()
            cancel = getattr(self.client, "cancel_request", None)
            if callable(cancel):
                cancel(event)

    def _cancel_move_requests(self, events):
        cancel = getattr(self.move_client, "cancel_request", None)
        if callable(cancel):
            for event in set(events):
                try:
                    cancel(event)
                except Exception:
                    pass  # Keep stale results blocked even if a transport has already closed.

    def request(self, question, state):
        if self._closed or not isinstance(question, str) or not isinstance(state, dict):
            return False
        if self._active:
            if question.strip():
                self._queued = (question[:2000], dict(state))
            return False
        question = question.strip()[:2000]
        try:
            settings = self.settings.load_api_settings()
            key = self.settings.get_api_key()
            settings.validate(require_model=True)
            if not key:
                raise ValueError("No game commentary key")
            character_card = self._character_snapshot()
        except Exception:
            if question:
                self.controller.post_commentary(
                    "本地对弈和提示都可以正常使用。想和我边下边聊，可以先在设置中配置聊天模型。", "neutral")
            return False
        revision = self._identity(state)
        cancellation = threading.Event()
        self._active = (revision, cancellation)
        self._busy(True)
        messages = [{"role": "system", "content": _character_prompt(self._game_prompt, character_card)},
                    {"role": "user", "content": "真实对局信息：\n" + json.dumps(self._context(state), ensure_ascii=False)}]
        recent_dialogue = _recent_dialogue(state)
        messages.extend(recent_dialogue)
        # The controller already appended this user's question. Do not send it
        # twice, while keeping a clear final request for automatic reactions.
        if not messages or messages[-1].get("content") != question:
            messages.append({"role": "user", "content": question or
                             "这是自动观察到的一个回合。结合最近对话和真实局面决定是否有新反应，普通一步可以安静，reply留空。"})
        messages = [{"role": row["role"], "content": redact_secret(row["content"], key)} for row in messages]

        def work():
            if not question:
                raw = self.client.request_text(settings, key, messages, output_tokens=500,
                                               cancellation_event=cancellation)
                value = _json_response(raw)
                if value is None:
                    return None
                text = redact_secret(value.get("reply", ""), key)
                return ChatReply(_fresh_reaction(text, recent_dialogue), normalize_emotion(value.get("emotion")))
            return self.client.complete_context(settings, key, messages, output_tokens=900,
                                                cancellation_event=cancellation)

        def done(reply, error):
            self._active = None
            if self._closed:
                return
            self._busy(False)
            current = self.controller.snapshot()
            if self._identity(current) == revision and not cancellation.is_set():
                if reply is not None and not error and reply.reply.strip():
                    self.controller.post_commentary(reply.reply, reply.emotion)
                elif question:
                    self.controller.post_commentary("这次讲解暂时没连上，棋局已经保留，可以稍后再问我。", "neutral")
            queued, self._queued = self._queued, None
            if queued and self._identity(queued[1]) == self._identity(self.controller.snapshot()):
                self.request(*queued)

        self.runner.submit(work, done)
        return True

    def shutdown(self):
        with self._move_lock:
            if self._closed:
                return
            self._closed = True
            pending = [event for _, event in self._move_requests]
            self._move_requests = []
            for event in pending:
                event.set()
        self._cancel_move_requests(pending)
        self._queued = None
        if self._active:
            self._active[1].set()
        self.client.shutdown()
        self.move_client.shutdown()
        self.runner.shutdown()
