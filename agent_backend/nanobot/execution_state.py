"""Per-call execution identities and explicit uncertainty, independent of SDKs."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json

CURRENT_CALL = ContextVar("aura_native_tool_call", default=None)


class ExecutionConflict(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def action_identity(task_id, call_id):
    return hashlib.sha256((str(task_id) + ":" + str(call_id)).encode("utf-8")).hexdigest()


class EffectLedger:
    """An intent is unique within a task; a new action is not a replay.

    The host's durable checkpoint owns process-loss recovery. This table owns
    same-process idempotency and prevents switching tools to evade uncertainty.
    No tool arguments or credentials are emitted by this class.
    """

    def __init__(self):
        self.records = {}
        self.uncertain = set()

    def begin(self, identity, name, arguments, *, domain="none"):
        fingerprint = hashlib.sha256(json.dumps(
            [name, arguments], sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        old = self.records.get(identity)
        if old is not None:
            if old["fingerprint"] != fingerprint:
                raise ExecutionConflict("ACTION_ID_CONFLICT")
            if old["state"] == "running":
                raise ExecutionConflict("ACTION_ALREADY_RUNNING")
            return deepcopy(old)
        if domain != "none" and domain in self.uncertain:
            raise ExecutionConflict("ACTION_OUTCOME_UNCERTAIN")
        self.records[identity] = {"fingerprint": fingerprint, "name": name,
                                  "domain": domain, "state": "running", "result": None}
        return None

    def settle(self, identity, result, *, uncertain=False):
        entry = self.records[identity]
        entry["state"] = "uncertain" if uncertain else "confirmed"
        entry["result"] = result
        if uncertain and entry["domain"] != "none":
            self.uncertain.add(entry["domain"])

    def resolve(self, domain, *, observed=False):
        # A model string cannot clear an unknown effect. Only the execution
        # adapter's fresh, authoritative observation may confirm its outcome.
        if observed is not True:
            raise ExecutionConflict("FRESH_OBSERVATION_REQUIRED")
        self.uncertain.discard(domain)


def safe_error_code(exc):
    """Classify failures without forwarding raw provider text or request data."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code and len(code) <= 80 and all(
            ch.isupper() or ch.isdigit() or ch == "_" for ch in code):
        return code
    if isinstance(exc, FileNotFoundError):
        return "FILE_NOT_FOUND"
    if isinstance(exc, PermissionError):
        return "PERMISSION_DENIED"
    if isinstance(exc, UnicodeError):
        return "UNSUPPORTED_TEXT_ENCODING"
    if isinstance(exc, TimeoutError):
        return "REQUEST_TIMEOUT"
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "PROVIDER_AUTH_FAILED"
    if status == 429:
        return "PROVIDER_RATE_LIMITED"
    if isinstance(status, int) and status >= 500:
        return "PROVIDER_UNAVAILABLE"
    if isinstance(exc, ValueError):
        text = str(exc)
        if text and len(text) <= 80 and all(ch.isupper() or ch.isdigit() or ch == "_" for ch in text):
            return text
        return "INVALID_TOOL_ARGUMENTS"
    if isinstance(exc, OSError):
        return "IO_UNAVAILABLE"
    return "TOOL_FAILED"
