"""Host-only exact-worker closure evidence; no IPC deserialization or PID proof.

An opaque handle is issued for one Popen instance. A closure receipt is issued
only after that instance is reaped and its owned process tree confirms empty.
Unknown processes remain tracked. Cancellation merely requests termination.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import threading
import time


class WorkerRunHandle:
    __slots__ = ("_issuer", "_nonce", "_run_id", "_conversation_id", "_process")

    def __new__(cls, *args, **kwargs):
        raise TypeError("HOST_WORKER_HANDLE_REQUIRED")

    def __setattr__(self, *args): raise AttributeError("HOST_WORKER_HANDLE_IMMUTABLE")

    @property
    def run_id(self): return self._run_id

    @property
    def conversation_id(self): return self._conversation_id

    def __repr__(self): return "WorkerRunHandle([host-owned identity])"


class WorkerClosureReceipt:
    __slots__ = ("_issuer", "_handle", "_process")

    def __new__(cls, *args, **kwargs):
        raise TypeError("HOST_WORKER_CLOSURE_REQUIRED")

    def __setattr__(self, *args): raise AttributeError("HOST_WORKER_CLOSURE_IMMUTABLE")

    @property
    def run_id(self): return self._handle.run_id

    @property
    def conversation_id(self): return self._handle.conversation_id

    @property
    def worker_closed(self): return True

    @property
    def reaped(self): return True

    def __repr__(self): return "WorkerClosureReceipt([exact host-owned worker reaped])"


@dataclass
class _Run:
    handle: WorkerRunHandle
    process: object
    tree: object = None
    dispatched: bool = False
    receipt: WorkerClosureReceipt | None = None
    lock: object = field(default_factory=threading.Lock)


class WorkerLifecycle:
    def __init__(self):
        self._issuer = object()
        self._runs = {}
        self._lock = threading.Lock()

    def register(self, process, run_id, conversation_id):
        if (not callable(getattr(process, "wait", None)) or not callable(getattr(process, "kill", None))
                or not isinstance(run_id, str) or not run_id or
                conversation_id is not None and not isinstance(conversation_id, str)):
            raise ValueError("INVALID_HOST_WORKER")
        handle = object.__new__(WorkerRunHandle)
        for name, value in (("_issuer", self._issuer), ("_nonce", secrets.token_hex(32)),
                            ("_run_id", run_id), ("_conversation_id", conversation_id), ("_process", process)):
            object.__setattr__(handle, name, value)
        with self._lock:
            self._runs[handle._nonce] = _Run(handle, process)
            # Retain every unresolved run. Bound only already-confirmed history.
            complete = [key for key, run in self._runs.items() if run.receipt is not None]
            for key in complete[:-128]: self._runs.pop(key, None)
        return handle

    def _get(self, handle):
        if type(handle) is not WorkerRunHandle:
            raise ValueError("INVALID_HOST_WORKER_HANDLE")
        with self._lock:
            record = self._runs.get(getattr(handle, "_nonce", None))
        if (record is None or record.handle is not handle or handle._issuer is not self._issuer
                or record.process is not handle._process):
            raise ValueError("INVALID_HOST_WORKER_HANDLE")
        return record

    def attach_tree(self, handle, tree):
        record = self._get(handle)
        if not callable(getattr(tree, "close", None)) or not callable(getattr(tree, "confirm_closed", None)):
            raise ValueError("INVALID_HOST_WORKER_TREE")
        with record.lock:
            if record.dispatched or record.tree is not None or record.receipt is not None:
                raise ValueError("HOST_WORKER_TREE_ALREADY_BOUND")
            record.tree = tree

    def mark_dispatched(self, handle):
        record = self._get(handle)
        with record.lock:
            if record.receipt is not None:
                raise ValueError("HOST_WORKER_ALREADY_CLOSED")
            # Set before the first stdin write: a partial failed write must not
            # be mistaken for proof that the worker never saw a run command.
            record.dispatched = True

    def request_stop(self, handle):
        record = self._get(handle)
        # A concurrent settle has already requested Stop. Never block a UI
        # cancellation callback waiting for the bounded reap operation.
        if not record.lock.acquire(blocking=False): return
        try:
            if record.receipt is not None: return
            try:
                if record.tree is not None: record.tree.close()
                else: record.process.kill()
            except Exception:
                try: record.process.kill()
                except Exception: pass
        finally:
            record.lock.release()

    def settle(self, handle, *, timeout_seconds=2.0):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 <= timeout_seconds <= 10):
            raise ValueError("INVALID_WORKER_CLEANUP_TIMEOUT")
        record = self._get(handle)
        with record.lock:
            if record.receipt is not None: return record.receipt
            deadline = time.monotonic() + timeout_seconds
            try:
                if record.tree is not None: record.tree.close()
                else: record.process.kill()
            except Exception:
                try: record.process.kill()
                except Exception: pass
            try:
                code = record.process.wait(timeout=max(0, deadline - time.monotonic()))
                if type(code) is not int: return None
            except Exception:
                return None
            # No containment after a run reached stdin cannot prove helpers
            # stopped. Before dispatch, this host entrypoint has not authorized
            # any tool/helper and exact Popen reaping is sufficient.
            if record.tree is None:
                tree_closed = not record.dispatched
            else:
                try:
                    tree_closed = record.tree.confirm_closed(timeout_seconds=max(0, deadline - time.monotonic())) is True
                except Exception:
                    tree_closed = False
            if not tree_closed: return None
            receipt = object.__new__(WorkerClosureReceipt)
            for name, value in (("_issuer", self._issuer), ("_handle", handle), ("_process", record.process)):
                object.__setattr__(receipt, name, value)
            record.receipt = receipt
            return receipt

    def receipt(self, handle):
        record = self._get(handle)
        with record.lock: return record.receipt

    def validates(self, handle, receipt):
        try: record = self._get(handle)
        except ValueError: return False
        return (type(receipt) is WorkerClosureReceipt and record.receipt is receipt
                and receipt._issuer is self._issuer and receipt._handle is handle
                and receipt._process is record.process)
