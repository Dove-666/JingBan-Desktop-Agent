"""Host-owned questions and scoped file grants; no SDK or UI imports."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import threading
import time
from desktop_pet.redaction import redact_text

_IDENTITY = re.compile(r"interaction-[a-f0-9]{32}\Z")


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or not empty and not value.strip()
            or any(ord(ch) < 32 and ch not in "\n\t" for ch in value)):
        raise ValueError("INVALID_INTERACTION")
    return value.strip()


def file_grant(value):
    if not isinstance(value, dict) or set(value) != {"path", "access", "directory"}:
        raise ValueError("INVALID_FILE_GRANT")
    path = _text(value["path"], 4096)
    if value["access"] not in {"read", "write"} or type(value["directory"]) is not bool:
        raise ValueError("INVALID_FILE_GRANT")
    if path.startswith(("\\\\", "//")) or "\x00" in path:
        raise ValueError("INVALID_FILE_GRANT")
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError("INVALID_FILE_GRANT")
    target = target.resolve()
    if value["directory"]:
        if not target.is_dir():
            raise ValueError("FILE_GRANT_UNAVAILABLE")
    elif target.exists():
        if not target.is_file():
            raise ValueError("FILE_GRANT_UNAVAILABLE")
    elif value["access"] != "write" or not target.parent.is_dir():
        raise ValueError("FILE_GRANT_UNAVAILABLE")
    if target == Path(target.anchor):
        raise ValueError("CHOOSE_A_SPECIFIC_FOLDER")
    return {"path": str(target), "access": value["access"], "directory": value["directory"]}


def checked_interaction(frame, *, secrets=(), resolve_resource=True):
    if (not isinstance(frame, dict) or not isinstance(frame.get("interaction_id"), str)
            or not _IDENTITY.fullmatch(frame["interaction_id"])
            or frame.get("kind") not in {"question", "file_access"}):
        raise ValueError("INVALID_INTERACTION")
    def clean(value, limit):
        value = _text(value, limit)
        return redact_text(value, secrets, "[已隐藏密钥]")
    result = {"id": frame["interaction_id"], "kind": frame["kind"],
              "question": clean(frame.get("question"), 16000)}
    if result["kind"] == "question":
        options = frame.get("options", [])
        if not isinstance(options, list) or len(options) > 6:
            raise ValueError("INVALID_INTERACTION")
        result["options"] = [clean(option, 240) for option in options]
    else:
        resource = frame.get("resource")
        if (not isinstance(resource, dict) or set(resource) != {"path", "access", "directory"}
                or resource.get("access") not in {"read", "write"} or type(resource.get("directory")) is not bool):
            raise ValueError("INVALID_FILE_GRANT")
        _text(resource.get("path"), 4096)
        result["resource"] = file_grant(resource) if resolve_resource else deepcopy(resource)
    return result


class InteractionBroker:
    """A pending answer is task-bound, revocable and never inferred from time."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._pending = None

    def open(self, value, *, conversation_id, task_id, lifetime=1800):
        with self._lock:
            if self._pending is not None:
                raise ValueError("INTERACTION_ALREADY_PENDING")
            self._pending = {**deepcopy(value), "conversation_id": conversation_id,
                             "task_id": task_id, "deadline": self._clock() + lifetime}
            return self.public(conversation_id)

    def public(self, conversation_id=None):
        with self._lock:
            value = self._pending
            if (value is None or self._clock() >= value["deadline"]
                    or conversation_id is not None and conversation_id != value["conversation_id"]):
                return None
            return {key: deepcopy(item) for key, item in value.items() if key not in {"task_id", "deadline"}}

    def answer(self, conversation_id, identity, answer):
        if not isinstance(answer, dict) or set(answer) - {"text", "allowed"}:
            raise ValueError("INVALID_INTERACTION_ANSWER")
        with self._lock:
            pending = self._pending
            if (pending is None or pending["conversation_id"] != conversation_id
                    or pending["id"] != identity or self._clock() >= pending["deadline"]):
                return None
            if pending["kind"] == "file_access":
                if set(answer) != {"allowed"} or type(answer["allowed"]) is not bool:
                    raise ValueError("INVALID_INTERACTION_ANSWER")
                result = {"allowed": answer["allowed"]}
                if answer["allowed"]:
                    # Resolve again immediately before granting: the target may
                    # have changed while the user was looking at the question.
                    result["grant"] = file_grant(pending["resource"])
            else:
                if set(answer) != {"text"}:
                    raise ValueError("INVALID_INTERACTION_ANSWER")
                result = {"text": _text(answer["text"], 8000, empty=True),
                          "declined": not bool(answer["text"].strip())}
            self._pending = None
            return {"task_id": pending["task_id"], "interaction_id": identity, "answer": result}

    def expired(self):
        with self._lock:
            if self._pending is None or self._clock() < self._pending["deadline"]:
                return None
            pending, self._pending = self._pending, None
            return {"task_id": pending["task_id"], "interaction_id": pending["id"],
                    "answer": {"allowed": False, "expired": True}}

    def clear(self, task_id=None):
        with self._lock:
            if self._pending is not None and (task_id is None or self._pending["task_id"] == task_id):
                self._pending = None
