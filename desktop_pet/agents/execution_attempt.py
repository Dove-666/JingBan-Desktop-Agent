"""Opaque host proof that a finished invocation never attempted worker launch.

This is deliberately separate from exact-worker closure. Once a launch was
attempted, even if Popen raised, this registry can never prove no launch. Worker
ownership/cleanup remains with WorkerLifecycle and its unresolved-run registry.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading


class HostExecutionAttempt:
    __slots__ = ("_issuer", "_nonce", "_conversation_id")

    def __new__(cls, *args, **kwargs):
        raise TypeError("HOST_EXECUTION_ATTEMPT_REQUIRED")

    def __setattr__(self, *args): raise AttributeError("HOST_EXECUTION_ATTEMPT_IMMUTABLE")

    @property
    def conversation_id(self): return self._conversation_id

    def __repr__(self): return "HostExecutionAttempt([host-owned invocation])"


class NoWorkerLaunchReceipt:
    __slots__ = ("_issuer", "_attempt")

    def __new__(cls, *args, **kwargs):
        raise TypeError("HOST_NO_LAUNCH_RECEIPT_REQUIRED")

    def __setattr__(self, *args): raise AttributeError("HOST_NO_LAUNCH_RECEIPT_IMMUTABLE")

    @property
    def conversation_id(self): return self._attempt.conversation_id

    def __repr__(self): return "NoWorkerLaunchReceipt([finished without launch attempt])"


@dataclass
class _Attempt:
    handle: HostExecutionAttempt
    owner: object = None
    launch_attempted: bool = False
    finished: bool = False
    receipt: NoWorkerLaunchReceipt | None = None


class ExecutionAttempts:
    """No IO/threads; retain all in-flight identities and bounded terminal history."""
    def __init__(self):
        self._issuer = object()
        self._attempts = {}
        self._lock = threading.Lock()

    def begin(self, conversation_id):
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ValueError("HOST_EXECUTION_CONVERSATION_INVALID")
        handle = object.__new__(HostExecutionAttempt)
        for name, value in (("_issuer", self._issuer), ("_nonce", secrets.token_hex(32)),
                            ("_conversation_id", conversation_id)):
            object.__setattr__(handle, name, value)
        with self._lock:
            self._attempts[handle._nonce] = _Attempt(handle)
        return handle

    def _get(self, handle):
        # Caller holds _lock. Exact identity, not a serializable claim, is proof.
        if type(handle) is not HostExecutionAttempt:
            raise ValueError("HOST_EXECUTION_ATTEMPT_INVALID")
        record = self._attempts.get(getattr(handle, "_nonce", None))
        if record is None or record.handle is not handle or handle._issuer is not self._issuer:
            raise ValueError("HOST_EXECUTION_ATTEMPT_INVALID")
        return record

    def valid(self, handle):
        with self._lock:
            try: record = self._get(handle)
            except ValueError: return False
            return not record.finished

    def bind(self, handle, owner):
        with self._lock:
            try: record = self._get(handle)
            except ValueError: return False
            if record.finished or record.owner is not None or type(owner) is not object:
                return False
            record.owner = owner  # Only an opaque token; do not retain request data.
            return True

    def mark_launch_attempted(self, handle):
        with self._lock:
            record = self._get(handle)
            if record.finished or record.launch_attempted:
                raise ValueError("HOST_EXECUTION_ATTEMPT_ALREADY_COMMITTED")
            # Must run BEFORE Popen. A thrown Popen is still an attempted launch.
            record.launch_attempted = True

    def finish(self, handle):
        with self._lock:
            record = self._get(handle)
            if record.finished: return
            record.finished = True
            if not record.launch_attempted:
                receipt = object.__new__(NoWorkerLaunchReceipt)
                object.__setattr__(receipt, "_issuer", self._issuer)
                object.__setattr__(receipt, "_attempt", handle)
                record.receipt = receipt
            # The no-launch question is settled for finished attempted calls too:
            # always denied. Their unresolved workers stay in WorkerLifecycle;
            # pruning this independent history never releases a worker registry.
            # Retention follows completion order, not begin order. A long-lived
            # invocation must not lose its newly issued proof on its own seal.
            self._attempts.pop(handle._nonce)
            self._attempts[handle._nonce] = record
            terminal = [key for key, value in self._attempts.items() if value.finished]
            for key in terminal[:-128]: self._attempts.pop(key, None)

    def receipt(self, handle):
        with self._lock:
            try: return self._get(handle).receipt
            except ValueError: return None

    def validates(self, handle, receipt, *, owner=None):
        with self._lock:
            try: record = self._get(handle)
            except ValueError: return False
            return (type(receipt) is NoWorkerLaunchReceipt and record.receipt is receipt
                    and record.finished and not record.launch_attempted
                    and (owner is None or type(owner) is object and record.owner is owner)
                    and receipt._issuer is self._issuer and receipt._attempt is handle)
