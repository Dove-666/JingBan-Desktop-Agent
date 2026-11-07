"""Deliver worker results on Qt's GUI thread; never perform disk I/O in a view."""
from uuid import uuid4
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Signals(QObject):
    finished = Signal(str, object, str)


class _DrainState:
    """Track actual runnable completion independently of queued Qt callbacks."""
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = 0
        self._finalizer = None

    def add(self):
        with self._lock: self._pending += 1

    def complete(self):
        finalizer = None
        with self._lock:
            self._pending -= 1
            if self._pending == 0:
                finalizer, self._finalizer = self._finalizer, None
        if finalizer is not None: finalizer()

    def after_drain(self, finalizer):
        if finalizer is None: return
        with self._lock:
            if self._pending:
                self._finalizer = finalizer
                return
        finalizer()


class _Task(QRunnable):
    def __init__(self, identifier, function, drain):
        super().__init__()
        self.identifier = identifier
        self.function = function
        self._drain = drain
        self._state_lock = threading.Lock()
        self._started = False
        self._completed = False
        self.signals = _Signals()

    def _complete_once(self):
        with self._state_lock:
            if self._completed: return
            self._completed = True
        self._drain.complete()

    def start_failed(self):
        # A start exception does not prove that Qt rejected this exact worker.
        # Cancel its function under the same lock as run's entry, or wait for
        # the already-entered function's real finally. Never decrement twice.
        with self._state_lock:
            if self._started: return
            self.function = None
        self._complete_once()

    @Slot()
    def run(self):
        result, error = None, ""
        with self._state_lock:
            self._started = True
            function = self.function
        try:
            if function is not None:
                result = function()
        except Exception as exc:
            error = str(exc) if type(exc).__module__.startswith("desktop_pet.") else "操作失败，请检查本地存储、配置或网络后重试。"
        finally:
            with self._state_lock: self.function = None
            # The real function has ended, even if the GUI event loop has
            # stopped and its queued finished signal can never be delivered.
            try: self._complete_once()
            except Exception: pass
        try:
            self.signals.finished.emit(self.identifier, result, error)
        except RuntimeError:
            pass  # The application may have destroyed its Qt receiver on exit.


class TaskRunner(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = {}
        self._closed = False
        self._drain = _DrainState()

    def submit(self, function, callback):
        if self._closed:
            return None
        identifier = str(uuid4())
        worker = _Task(identifier, function, self._drain)
        self._tasks[identifier] = (worker, callback)
        worker.signals.finished.connect(self._finished)
        self._drain.add()
        try: QThreadPool.globalInstance().start(worker)
        except BaseException:
            # Keep the exact runnable alive if start accepted it before raising.
            # Its eventual signal may release this entry, but must not deliver
            # a success callback to a caller that already received the error.
            self._tasks[identifier] = (worker, None)
            worker.start_failed()
            raise
        return identifier

    @Slot(str, object, str)
    def _finished(self, identifier, result, error):
        entry = self._tasks.pop(identifier, None)
        if entry and not self._closed and callable(entry[1]):
            entry[1](result, error)

    def shutdown(self, *, on_drained=None):
        # Finalizers must be Qt-free: they may run in the final accepted worker
        # thread. This does not wait for that worker on the GUI thread.
        if self._closed: return
        self._closed = True
        self._drain.after_drain(on_drained)
