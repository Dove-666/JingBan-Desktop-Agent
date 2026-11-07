"""Public execution history, timing and redacted request context.

This records application phases and actual tool notifications. It never reads
model reasoning fields, images or arbitrary worker diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import threading
import time


STATES = {"running", "completed", "failed", "cancelled", "needs_user", "interrupted"}
TERMINAL_STATES = STATES - {"running"}
KINDS = {"progress", "tool", "computer", "summary"}
EVENT_STATUSES = {"running", "completed", "failed"}
SOURCE_NAMES = {"system", "history", "summary", "events", "memories", "selection", "current"}
_DATA_URL = re.compile(r"data:[^\s\"'<>]*", re.I)
_SUMMARY = {
    "completed": "回复已完成", "failed": "本次请求未能完成", "cancelled": "本次请求已停止",
    "needs_user": "任务未完成", "interrupted": "上次请求意外中断",
}


@dataclass(frozen=True)
class ExecutionViewUpdate:
    """An immutable public trace notification for an existing message."""

    conversation_id: str
    message_id: str
    execution_json: str


def _text(value, redact=lambda text: text):
    return redact(_DATA_URL.sub("[图片内容已省略]", value)) if isinstance(value, str) else ""


def _integer(value, default=0):
    return value if type(value) is int and value >= 0 else default


def _date(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def text_messages(messages, redact=lambda text: text):
    """Whitelist public text parts; image bytes and reasoning never enter storage."""
    result = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text", "output_text"}:
                    parts.append(_text(part.get("text"), redact))
                elif part.get("type") in {"image_url", "input_image", "image"}:
                    parts.append("[图片内容已省略]")
            content = "\n".join(parts)
        elif isinstance(content, str):
            content = _text(content, redact)
        else:
            continue
        result.append({"role": message["role"], "content": content})
    return result


def normalize_execution(value, redact=lambda text: text):
    """Copy only the public schema, without truncating the actual text context."""
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("state") not in STATES or _date(value.get("started_at")) is None):
        raise ValueError("invalid execution history")
    result = {"version": 1, "state": value["state"], "started_at": value["started_at"],
              "duration_ms": _integer(value.get("duration_ms")), "events": []}
    for event in value.get("events", []):
        if (not isinstance(event, dict) or event.get("kind") not in KINDS
                or event.get("status") not in EVENT_STATUSES
                or not isinstance(event.get("id"), str) or not event["id"]):
            continue
        clean = {"id": _text(event["id"], redact), "elapsed_ms": _integer(event.get("elapsed_ms")),
                 "kind": event["kind"], "label": _text(event.get("label"), redact), "status": event["status"]}
        if isinstance(event.get("detail"), str):
            clean["detail"] = _text(event["detail"], redact)
        result["events"].append(clean)
    context = value.get("context") if isinstance(value.get("context"), dict) else {}
    result["context"] = {
        "model": _text(context.get("model"), redact),
        "policy": "manual" if context.get("policy") == "manual" else "model",
        "history_messages": _integer(context.get("history_messages")),
        "estimated_input_tokens": _integer(context.get("estimated_input_tokens")),
        "model_context_tokens": _integer(context.get("model_context_tokens"), None),
        "output_tokens": _integer(context.get("output_tokens"), None),
        "source_counts": {name: _integer(count) for name, count in context.get("source_counts", {}).items()
                          if name in SOURCE_NAMES} if isinstance(context.get("source_counts"), dict) else {},
        "messages": text_messages(context.get("messages"), redact),
    }
    for name in ("agent_model", "gui_model"):
        if isinstance(context.get(name), str) and context[name]:
            result["context"][name] = _text(context[name], redact)
    return result


def merge_execution(current, incoming):
    """A late progress snapshot cannot replace a terminal or newer attempt."""
    incoming = normalize_execution(incoming)
    if not isinstance(current, dict):
        return incoming
    try:
        current = normalize_execution(current)
    except ValueError:
        return incoming
    if _date(incoming["started_at"]) < _date(current["started_at"]):
        return current
    if incoming["started_at"] != current["started_at"]:
        return incoming
    if current["state"] == "cancelled" and incoming["state"] != "cancelled":
        return current
    if current["state"] in TERMINAL_STATES and incoming["state"] == "running":
        return current
    if incoming["duration_ms"] < current["duration_ms"]:
        return current
    return incoming


def interrupted_execution(execution):
    """Finish an orphaned persisted trace without replaying any operation."""
    value = normalize_execution(execution)
    if value["state"] != "running":
        return value
    elapsed = max(value["duration_ms"], int((datetime.now(timezone.utc) - _date(value["started_at"])).total_seconds() * 1000))
    value.update(state="interrupted", duration_ms=max(0, elapsed))
    for event in value["events"]:
        if event["status"] == "running":
            event["status"] = "failed"
    value["events"].append({"id": "summary:interrupted", "elapsed_ms": value["duration_ms"],
                            "kind": "summary", "label": _SUMMARY["interrupted"], "status": "failed"})
    return value


class ExecutionTrace:
    def __init__(self, model, redact=lambda text: text):
        self._lock = threading.RLock()
        self._clock = time.monotonic()
        self._redact = redact
        self._value = {"version": 1, "state": "running",
                       "started_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                       "duration_ms": 0, "events": [],
                       "context": {"model": model, "policy": "model", "messages": []}}
        self.event("phase:starting", "progress", "开始处理请求")

    def _elapsed(self):
        return max(0, int((time.monotonic() - self._clock) * 1000))

    def snapshot(self):
        with self._lock:
            if self._value["state"] == "running":
                self._value["duration_ms"] = self._elapsed()
            return normalize_execution(self._value, self._redact)

    def event(self, event_id, kind, label, status="running", *, step=None, detail=None):
        with self._lock:
            if (self._value["state"] != "running" or kind not in KINDS or status not in EVENT_STATUSES
                    or not isinstance(event_id, str) or not event_id or not isinstance(label, str)):
                return False
            elapsed = self._elapsed()
            events = self._value["events"]
            existing = next((event for event in events if event["id"] == event_id), None)
            if existing is not None:
                if existing["status"] in {"completed", "failed"} and status == "running":
                    return False
                existing.update(label=label, status=status)
            else:
                for event in events:
                    if event["kind"] == "progress" and event["status"] == "running":
                        event["status"] = "completed"
                existing = {"id": event_id, "elapsed_ms": elapsed, "kind": kind, "label": label, "status": status}
                events.append(existing)
            if type(step) is int and step >= 0:
                existing["detail"] = f"第 {step} 步"
            if isinstance(detail, str) and detail:
                existing["detail"] = detail
            self._value["duration_ms"] = elapsed
            return True

    def context(self, messages, result, model):
        with self._lock:
            if self._value["state"] != "running":
                return
            clean = text_messages(messages, self._redact)
            history = max(0, sum(message["role"] in {"user", "assistant"} for message in clean) - 1)
            self._value["context"] = {
                "model": model, "policy": getattr(result, "policy", "manual"),
                "history_messages": getattr(result, "history_messages", history),
                "estimated_input_tokens": getattr(result, "estimated_input_tokens", 0),
                "model_context_tokens": getattr(result, "model_context_tokens", None),
                "output_tokens": getattr(result, "output_reserve", None),
                "source_counts": getattr(result, "source_counts", {"history": history, "current": 1}),
                "messages": clean,
            }

    def models(self, agent_model, gui_model=""):
        with self._lock:
            if self._value["state"] == "running":
                self._value["context"]["agent_model"] = agent_model
                if gui_model:
                    self._value["context"]["gui_model"] = gui_model

    def finish(self, state, *, replace_completed=False):
        if state not in TERMINAL_STATES:
            raise ValueError("invalid execution outcome")
        with self._lock:
            if (replace_completed and state == "failed"
                    and self._value["state"] in {"completed", "needs_user"}):
                self._value["events"] = [event for event in self._value["events"]
                                         if event["id"] not in {"summary:completed", "summary:needs_user"}]
                self._value["state"] = "running"
            if self._value["state"] != "running":
                return self.snapshot()
            status = "completed" if state in {"completed", "needs_user"} else "failed"
            self._value["duration_ms"] = self._elapsed()
            for event in self._value["events"]:
                if event["status"] == "running":
                    event["status"] = status
            self._value["events"].append({"id": "summary:" + state, "elapsed_ms": self._value["duration_ms"],
                                          "kind": "summary", "label": _SUMMARY[state], "status": status})
            self._value["state"] = state
            return self.snapshot()
