"""An owned, bounded JSONL first write with an explicit one-byte commit.

The trusted reader has not received a complete command before LF. One watchdog
belongs to this exact run; it requests only that run's termination on deadline
or cancellation. Errors never contain payload/provider text.
"""
from __future__ import annotations

import threading
import time


class StagedInputError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StagedRunInput:
    def __init__(self, pipe, *, stop_owned, cancellation_event, stage_timeout=10.0, commit_timeout=1.0):
        if (not callable(getattr(pipe, "write", None)) or not callable(stop_owned)
                or not callable(getattr(cancellation_event, "is_set", None))
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not 0 < value <= 30 for value in (stage_timeout, commit_timeout))):
            raise ValueError("INVALID_STAGED_INPUT")
        self._pipe, self._stop_owned, self._cancel = pipe, stop_owned, cancellation_event
        self._stage_timeout, self._commit_timeout = stage_timeout, commit_timeout
        self._condition = threading.Condition()
        self._deadline = None
        self._phase = None
        self._failure = None
        self._closed = False
        self._staged = self._committed = False
        self._thread = None
        self._active_writes = 0

    def __repr__(self): return "StagedRunInput([owned bounded input])"

    def start(self):
        with self._condition:
            if self._closed or self._thread is not None: raise StagedInputError("WORKER_INPUT_STATE_INVALID")
            self._thread = threading.Thread(target=self._watch, name="aura-owned-run-input-deadline", daemon=True)
            self._thread.start()

    def _watch(self):
        while True:
            with self._condition:
                if self._closed: return
                expired = self._deadline is not None and time.monotonic() >= self._deadline
                if self._cancel.is_set() or expired:
                    self._failure = ("WORKER_INPUT_CANCELLED" if self._cancel.is_set() else
                        "WORKER_INPUT_COMMIT_TIMEOUT" if self._phase == "commit" else "WORKER_INPUT_STAGE_TIMEOUT")
                    break
                remaining = max(0, self._deadline - time.monotonic()) if self._deadline is not None else .05
                self._condition.wait(timeout=min(.05, remaining))
        try: self._stop_owned()
        except Exception: pass

    def _begin(self, phase, seconds):
        with self._condition:
            if self._closed or self._thread is None or self._failure or self._phase is not None or self._active_writes:
                raise StagedInputError(self._failure or "WORKER_INPUT_STATE_INVALID")
            if self._cancel.is_set(): raise StagedInputError("WORKER_INPUT_CANCELLED")
            self._phase, self._deadline = phase, time.monotonic() + seconds
            self._condition.notify_all()

    def _check(self):
        with self._condition:
            code = self._failure
            if self._cancel.is_set(): code = "WORKER_INPUT_CANCELLED"
            if code is None and self._deadline is not None and time.monotonic() >= self._deadline:
                code = "WORKER_INPUT_COMMIT_TIMEOUT" if self._phase == "commit" else "WORKER_INPUT_STAGE_TIMEOUT"
            if code: raise StagedInputError(code)

    def _write(self, raw):
        view = memoryview(raw)
        offset = 0
        with self._condition: self._active_writes += 1
        try:
            while offset < len(view):
                self._check()
                count = self._pipe.write(view[offset:])
                if type(count) is not int or count <= 0 or count > len(view) - offset:
                    raise StagedInputError("WORKER_INPUT_WRITE_FAILED")
                offset += count
            self._pipe.flush()
            self._check()
        except StagedInputError as exc:
            with self._condition:
                if self._failure is None: self._failure=exc.code
            raise
        except Exception:
            try: self._check()
            except StagedInputError as exc:
                with self._condition:
                    if self._failure is None: self._failure=exc.code
                raise
            with self._condition:
                if self._failure is None: self._failure="WORKER_INPUT_WRITE_FAILED"
            raise StagedInputError("WORKER_INPUT_WRITE_FAILED") from None
        finally:
            with self._condition:
                self._active_writes -= 1
                self._deadline, self._phase = None, None
                self._condition.notify_all()

    def stage(self, raw):
        if not isinstance(raw, bytes) or not raw or b'\n' in raw or b'\r' in raw or self._staged or self._committed:
            raise StagedInputError("WORKER_INPUT_FRAME_INVALID")
        self._begin("stage", self._stage_timeout)
        self._write(raw)
        self._staged = True

    def commit(self):
        if not self._staged or self._committed:
            raise StagedInputError("WORKER_INPUT_STATE_INVALID")
        self._begin("commit", self._commit_timeout)
        self._write(b'\n')
        self._committed = True

    def close(self, *, timeout_seconds=1.0):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 <= timeout_seconds <= 10:
            raise ValueError("INVALID_INPUT_WATCHDOG_TIMEOUT")
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread.ident is not None and thread is not threading.current_thread(): thread.join(timeout_seconds)
        with self._condition:
            return (thread is None or not thread.is_alive()) and self._active_writes == 0
