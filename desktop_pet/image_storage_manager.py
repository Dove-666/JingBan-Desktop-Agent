"""On-demand native image management; all storage work stays off the Qt thread."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal

from .async_tasks import TaskRunner
from .chat_images import IMAGE_ID
from .image_storage import ImageStorageGateError


_ERRORS = {
    "unavailable": "图片存储暂时不可用，请稍后重试。",
    "busy": "图片或任务仍在使用，或退出状态尚未确认，请稍后重试。",
    "changed": "图片或引用记录已变化，请刷新后重新确认。",
    "storage": "无法读取图片存储，请检查本地存储后重试。",
    "delete": "删除结果未能确认，请刷新图片列表后查看。",
    "closed": "应用正在关闭，本次操作未继续。",
}
_ROW_FIELDS = ("id", "sha256", "size", "mime_type", "width", "height", "created_at", "filename")


def _failure(code):
    return {"ok": False, "code": code, "message": _ERRORS.get(code, _ERRORS["storage"])}


def _signature(image, references):
    # This is a confirmation of content identity and current metadata, not a
    # filesystem generation or an atomic defense against external file edits.
    value = {"image": {key: image[key] for key in _ROW_FIELDS}, "references": references}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class ImageStorageManager(QObject):
    """One host-owned service; dialogs may disappear without cancelling real IO.

    Callbacks receive ``(result, error)`` once, on Qt. Fixed failures are in the
    result, with no raw storage exceptions or private paths. Closing a dialog
    does not close this service. Controller shutdown cancels pending work while
    an entered gate.run retains its blocker through physical completion.
    """

    _fallback = Signal(object)

    def __init__(self, chat):
        super().__init__(chat)
        self.chat = chat
        self._gate = chat.image_storage_gate
        self._owner = self._gate.create_owner()
        self._runner = TaskRunner(self)
        self._closed = False
        self._tickets = OrderedDict()
        self._fallback.connect(self._deliver_fallback, Qt.ConnectionType.QueuedConnection)
        chat.image_storage_changed.connect(self._host_changed)
        gate, owner = self._gate, self._owner
        self.destroyed.connect(lambda: gate.close_owner(owner))

    def _host_changed(self):
        if getattr(self.chat, "_closed", False):
            self.shutdown()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._tickets.clear()
        self._gate.close_owner(self._owner)
        self._runner.shutdown()

    def _live(self):
        return not self._closed and not getattr(self.chat, "_closed", True)

    def _inbox(self):
        if not self._live():
            raise RuntimeError("IMAGE_MANAGER_CLOSED")
        inbox = self.chat._image_inbox
        if inbox is None:
            raise RuntimeError("IMAGE_MANAGER_UNAVAILABLE")
        return inbox

    @staticmethod
    def _reject(callback, code):
        callback(_failure(code), "")
        return False

    def _deliver_fallback(self, completion):
        completion()

    def _notify(self):
        try:
            self.chat.notify_image_storage_changed()
        except RuntimeError:
            pass  # The Qt host can be destroyed while accepted IO drains.

    def _submit(self, operation, callback, *, token=None, failure_code="storage"):
        """Handle start-None/start-raise without guessing an accepted IO stopped."""
        lock = threading.Lock()
        state = {"done": False, "failed_start": False, "delivered": False, "result": None}

        def finished(result, error):
            with lock:
                if state["delivered"]:
                    return
                state["delivered"] = True
            if self._live():
                callback(result if isinstance(result, dict) else _failure(failure_code), "")

        def queue_fallback():
            with lock:
                result = state["result"]
            try:
                self._fallback.emit(lambda: finished(result, ""))
            except RuntimeError:
                pass  # No UI remains; physical work still retired its token.

        def physical():
            result = None
            try:
                def execute():
                    return operation() if self._live() else _failure("closed")
                result = self._gate.run(token, execute) if token is not None else execute()
                return result
            except Exception:
                result = _failure(failure_code)
                return result
            finally:
                with lock:
                    state["result"] = result
                    state["done"] = True
                    fallback = state["failed_start"]
                if fallback:
                    queue_fallback()
                if token is not None:
                    self._notify()

        try:
            accepted = self._runner.submit(physical, finished)
        except Exception:
            accepted = None
        if accepted is not None:
            return True
        with lock:
            state["failed_start"] = True
            done = state["done"]
        cancelled = self._gate.release(token) if token is not None else True
        if done or cancelled:
            queue_fallback()
        self._notify()
        return False

    def load_usage(self, callback):
        """The settings row needs totals only, without archive-reference scans."""
        if not self._live():
            return self._reject(callback, "closed")
        return self._submit(lambda: {"ok": True, "usage": self._inbox().usage()}, callback)

    def load_page(self, offset, callback):
        if not self._live():
            return self._reject(callback, "closed")
        if type(offset) is not int or offset < 0:
            return self._reject(callback, "unavailable")

        def read():
            inbox = self._inbox()
            page = inbox.list(offset, 20)
            references = self.chat.store.image_reference_summary([row["id"] for row in page["items"]])
            return {"ok": True, "usage": inbox.usage(), **page, "references": references}
        return self._submit(read, callback)

    def review_delete(self, image_id, callback):
        if not self._live():
            return self._reject(callback, "closed")
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            return self._reject(callback, "unavailable")

        def read():
            image = self._inbox().metadata(image_id)
            snapshot = self.chat.store.image_reference_snapshot([image_id])[image_id]
            references = {key: snapshot[key] for key in ("message_count", "conversation_count")}
            return {"ok": True, "image": image, "references": references,
                    "signature": _signature(image, snapshot)}

        def reviewed(result, error):
            if result.get("ok"):
                ticket = str(uuid4())
                self._tickets[ticket] = (image_id, result.pop("signature"))
                while len(self._tickets) > 64:
                    self._tickets.popitem(last=False)
                result["confirmation_id"] = ticket
            callback(result, error)
        return self._submit(read, reviewed)

    def discard_confirmation(self, confirmation_id):
        self._tickets.pop(confirmation_id, None)

    def _resources_idle(self):
        # Stop/busy flags are insufficient: direct processes, queue closure and
        # guardian resources must all be positively idle. Missing/failed probes
        # are unavailable, not a permission to delete.
        try:
            service = self.chat._agent_service
            coordinator = self.chat._agent_queue
            return (service.execution_resources_pending() is False and coordinator is not None
                    and coordinator.queue.has_image_maintenance_blocker() is False)
        except Exception:
            return False

    def delete(self, confirmation_id, callback):
        if not self._live():
            return self._reject(callback, "closed")
        confirmation = self._tickets.get(confirmation_id) if isinstance(confirmation_id, str) else None
        if confirmation is None:
            return self._reject(callback, "changed")
        try:
            token = self._gate.maintenance(self._owner)
        except ImageStorageGateError:
            return self._reject(callback, "busy")
        self._tickets.pop(confirmation_id, None)
        image_id, expected = confirmation
        self._notify()

        def remove():
            if not self._resources_idle():
                return _failure("busy")
            inbox = self._inbox()
            image = inbox.metadata(image_id)
            snapshot = self.chat.store.image_reference_snapshot([image_id])[image_id]
            if _signature(image, snapshot) != expected:
                return _failure("changed")
            if not self._live():
                return _failure("closed")
            try:
                result = inbox.delete(image_id)
            finally:
                # Even a failed acknowledgement can follow a committed change.
                # Invalidate before the physical lease is allowed to retire.
                self.chat.mark_image_storage_deleted(image_id)
            return {"ok": True, **result}
        return self._submit(remove, callback, token=token, failure_code="delete")
