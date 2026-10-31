"""Deliver worker results on Qt's GUI thread; never perform disk I/O in a view."""
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Signals(QObject):
    finished = Signal(str, object, str)


class _Task(QRunnable):
    def __init__(self, identifier, function):
        super().__init__()
        self.identifier = identifier
        self.function = function
        self.signals = _Signals()

    @Slot()
    def run(self):
        result, error = None, ""
        try:
            result = self.function()
        except Exception as exc:
            error = str(exc) if type(exc).__module__.startswith("desktop_pet.") else "操作失败，请检查本地存储、配置或网络后重试。"
        finally:
            self.function = None
        try:
            self.signals.finished.emit(self.identifier, result, error)
        except RuntimeError:
            pass  # The application may have destroyed its Qt receiver on exit.


class TaskRunner(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = {}
        self._closed = False

    def submit(self, function, callback):
        if self._closed:
            return None
        identifier = str(uuid4())
        worker = _Task(identifier, function)
        self._tasks[identifier] = (worker, callback)
        worker.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(worker)
        return identifier

    @Slot(str, object, str)
    def _finished(self, identifier, result, error):
        entry = self._tasks.pop(identifier, None)
        if entry and not self._closed:
            entry[1](result, error)

    def shutdown(self):
        # Accepted disk writes finish, while UI callbacks after exit are ignored.
        self._closed = True
