"""Actual RPC methods, owned SQLite receipts, fake image effects and threads."""
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.conversations import ConversationStore
from desktop_pet.image_storage import ImageStorageGate, ImageStorageGateError
from desktop_pet.agents.job_queue import HostJobReference, JobQueue
from desktop_pet.remote import controller as runtime
from desktop_pet.remote.controller import RemoteController
from desktop_pet.remote.errors import RemoteError
from desktop_pet.remote.sync import RemoteSyncStore


PEER = "a" * 64
IMAGE = "b" * 64


class ShortFuture(Future):
    def result(self, timeout=None):
        return super().result(timeout=.04 if timeout == 5 else timeout)


class FakeInbox:
    def __init__(self):
        self.uploads = self.deletes = 0
        self.on_metadata = lambda: None
        self.on_upload = lambda: None
        self.on_delete = lambda: None

    def metadata(self, image_id):
        self.on_metadata()
        return {"id": image_id}

    def upload(self, params):
        self.on_upload()
        self.uploads += 1
        return {"id": IMAGE}

    def delete(self, image_id):
        self.on_delete()
        self.deletes += 1
        return {"deleted": True, "id": image_id}

    def list(self, offset=0, limit=50):
        return {"items": [{"id": IMAGE}], "has_more": False, "next_offset": 1}

    def get(self, image_id):
        return {"id": image_id, "data_base64": ""}


class OwnedRpcHost:
    """Bind production methods without constructing Qt or real credentials."""
    def __init__(self, path):
        self._closed = False
        self._enabled = True
        self._generation = 1
        self._clock = time.time
        self._rate_lock = threading.Lock()
        self._rates = OrderedDict()
        self._futures_lock = threading.Lock()
        self._futures = set()
        self.authorized = True
        self.preferences = SimpleNamespace(authorized=lambda peer: peer == PEER and self.authorized)
        self.notifications = self.sends = self.admissions = 0
        self.deleted_marks = []
        self.mutated_marks = []
        self.images = FakeInbox()
        self.gate = ImageStorageGate()
        self.queue = JobQueue(path / "queue.sqlite3")
        self.queue.initialize()
        self.store = ConversationStore(path / "messages.sqlite3")
        self.sync = RemoteSyncStore(self.store)
        self.cid = str(uuid4())
        self.chat = SimpleNamespace(_closed=False, image_storage_gate=self.gate, store=self.store,
            current_conversation_id=self.cid, busy=False,
            notify_image_storage_changed=self.notify,
            image_storage_epoch=0,
            mark_image_storage_deleted=self.mark_deleted,
            mark_image_storage_mutated=self.mark_mutated,
            external_import_state=lambda cid: "ready",
            send=self.send, admit_remote_agent=self.admit,
            agent_readiness=lambda: {"available": True},
            _agent_service=SimpleNamespace(execution_resources_pending=lambda: False),
            _agent_queue=SimpleNamespace(queue=self.queue))
        self.session = SimpleNamespace(chat=self.chat)
        self.image_received = SimpleNamespace(emit=lambda _value: None)
        self._chat_imported = SimpleNamespace(emit=lambda _value: None)
        self.on_send = lambda: None
        self.ui_mode = "inline"
        self.ui_queue = []
        self.ui_threads = []
        self.ui_errors = []
        self.ui_entered = threading.Event()
        self.ui_finish = threading.Event()
        self._ui_request = SimpleNamespace(emit=self.emit_ui)

    def __getattr__(self, name):
        method = getattr(RemoteController, name)
        return MethodType(method, self)

    def thread(self):
        return runtime.QThread.currentThread() if self.ui_mode == "inline" else self

    def notify(self):
        self.notifications += 1

    def mark_deleted(self, image_id):
        self.deleted_marks.append(image_id)
        self.chat.image_storage_epoch += 1

    def mark_mutated(self, image_id):
        self.mutated_marks.append(image_id)
        self.chat.image_storage_epoch += 1

    def send(self, text, **kwargs):
        self.on_send()
        self.sends += 1
        return True

    def admit(self, peer, request, cid, text, **kwargs):
        self.admissions += 1
        return {"accepted": True, "conversation_id": cid, "message_id": str(uuid4()),
                "job_id": str(uuid4()), "queue_state": "queued", "execution_mode": "agent", "reply_delivery": "sync.pull"}

    def emit_ui(self, item):
        self.ui_queue.append(item)
        if self.ui_mode == "queued":
            return

        def run():
            try: self._run_ui_request(item)
            except BaseException as exc: self.ui_errors.append(type(exc).__name__)

        thread = threading.Thread(target=run, name="owned-rpc-storage-ui")
        self.ui_threads.append(thread)
        thread.start()
        assert self.ui_entered.wait(3)

    def rpc(self, method, params=None, request_id=None):
        return self.handle_request(PEER, {"id": request_id or str(uuid4()), "method": method, "params": params or {}})


class RemoteImageStorageGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-rpc-storage-")
        self.path = Path(self.temp.name)
        self.host = OwnedRpcHost(self.path)

    def tearDown(self):
        self.host.ui_finish.set()
        for thread in self.host.ui_threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.host.ui_errors, [])
        self.assertEqual(self.host.gate.snapshot()["activity_count"], 0)
        self.assertFalse(self.host.gate.snapshot()["maintenance_active"])
        target = self.path.resolve()
        self.assertEqual(target.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(target.name.startswith("aura-rpc-storage-"))
        self.temp.cleanup()

    def assert_maintenance_blocked(self):
        with self.assertRaises(ImageStorageGateError):
            self.host.gate.maintenance(self.host.gate.create_owner())

    def import_params(self):
        user = str(uuid4())
        return {"conversation_id": self.host.cid, "messages": [
            {"id": user, "role": "user", "content": "Owned imported question", "image_id": IMAGE,
             "created_at": "2026-11-06T00:00:00Z"},
            {"id": str(uuid4()), "role": "assistant", "content": "Owned imported answer", "reply_to": user,
             "created_at": "2026-11-06T00:00:01Z"}]}

    def test_maintenance_refuses_upload_before_operation_reservation(self):
        owner = self.host.gate.create_owner()
        maintenance = self.host.gate.maintenance(owner)
        request = str(uuid4())
        try:
            result = self.host.rpc("image.upload", {"filename": "owned.png", "data_base64": "QUJD"}, request)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error"]["code"], "busy")
            self.assertEqual(self.host.images.uploads, 0)
            self.assertIsNone(self.host.sync.get_operation(PEER, request))
        finally:
            self.host.gate.release(maintenance)

    def test_running_qt_after_rpc_timeout_retains_inner_activity(self):
        self.host.ui_mode = "running"

        def held_send():
            self.host.ui_entered.set()
            self.assertTrue(self.host.ui_finish.wait(3))

        self.host.on_send = held_send
        with patch.object(runtime, "Future", ShortFuture):
            result = self.host.rpc("chat.send", {"conversation_id": self.host.cid, "text": "Owned text"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "busy")
        try:
            self.assertEqual(self.host.gate.snapshot()["activity_count"], 1)
            self.assert_maintenance_blocked()
        finally:
            self.host.ui_finish.set()
            for thread in self.host.ui_threads: thread.join(4)
        self.assertEqual(self.host.sends, 1)
        self.assertEqual(self.host.gate.snapshot()["activity_count"], 0)

    def test_closed_chat_rejects_writes_even_with_empty_gate(self):
        self.host.chat._closed = True
        request = str(uuid4())
        result = self.host.rpc("image.upload", {"filename": "owned.png", "data_base64": "QUJD"}, request)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.host.images.uploads, 0)
        self.assertIsNone(self.host.sync.get_operation(PEER, request))

    def test_all_shared_writes_and_delete_refuse_before_reservation_during_maintenance(self):
        methods = [("chat.send", {"conversation_id": self.host.cid, "text": "Owned question"}),
                   ("agent.send", {"conversation_id": self.host.cid, "text": "Owned task"}),
                   ("chat.import", self.import_params()), ("desktop.snapshot", {}),
                   ("image.delete", {"id": IMAGE})]
        maintenance = self.host.gate.maintenance(self.host.gate.create_owner())
        try:
            for method, params in methods:
                with self.subTest(method=method):
                    request = str(uuid4())
                    result = self.host.rpc(method, params, request)
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["error"]["code"], "busy")
                    self.assertIsNone(self.host.sync.get_operation(PEER, request))
            self.assertEqual((self.host.sends, self.host.admissions, self.host.images.deletes), (0, 0, 0))
        finally:
            self.host.gate.release(maintenance)

    def test_all_shared_paths_hold_activity_through_metadata_sql_and_receipt(self):
        self.host.images.on_metadata = self.assert_maintenance_blocked
        self.host.images.on_upload = self.assert_maintenance_blocked
        self.host.on_send = self.assert_maintenance_blocked
        original_reserve = self.host.sync.reserve_operation
        original_complete = self.host.sync.complete_operation
        original_import = self.host.sync.import_exchange
        observed = []

        def reserve(*args, **kwargs):
            self.assert_maintenance_blocked()
            observed.append("reserve")
            return original_reserve(*args, **kwargs)

        def complete(*args, **kwargs):
            self.assert_maintenance_blocked()
            observed.append("receipt")
            return original_complete(*args, **kwargs)

        def imported(*args, **kwargs):
            self.assert_maintenance_blocked()
            result = original_import(*args, **kwargs)
            self.assert_maintenance_blocked()
            observed.append("import")
            return result

        observer, settings = object(), object()
        permissions = []

        def permission():
            self.assert_maintenance_blocked()
            self.assertEqual(self.host.gate.snapshot()["activity_count"], 2)
            permissions.append(True)
            return observer, settings

        with patch.object(self.host.sync, "reserve_operation", side_effect=reserve), \
                patch.object(self.host.sync, "complete_operation", side_effect=complete), \
                patch.object(self.host.sync, "import_exchange", side_effect=imported), \
                patch.object(self.host, "_desktop_capture_permission", side_effect=permission), \
                patch.object(runtime, "capture_desktop_image", return_value={"data_base64": "QUJD"}):
            methods = [("image.upload", {"filename": "owned.png", "data_base64": "QUJD"}),
                       ("chat.send", {"conversation_id": self.host.cid, "text": "Owned question", "image_id": IMAGE}),
                       ("agent.send", {"conversation_id": self.host.cid, "text": "Owned task", "image_id": IMAGE}),
                       ("chat.import", self.import_params()), ("desktop.snapshot", {})]
            for method, params in methods:
                result = self.host.rpc(method, params)
                self.assertTrue(result["ok"], (method, result))
        self.assertEqual(observed.count("reserve"), 4)
        self.assertEqual(observed.count("receipt"), 4)
        self.assertEqual(observed.count("import"), 1)
        self.assertEqual(len(permissions), 2)
        self.assertEqual(self.host.images.uploads, 2)
        self.assertEqual(len(self.host.store.load_messages(self.host.cid)), 2)

    def test_pending_timed_out_qt_callback_is_cancelled_without_late_effect(self):
        self.host.ui_mode = "queued"
        with patch.object(runtime, "Future", ShortFuture):
            result = self.host.rpc("chat.send", {"conversation_id": self.host.cid, "text": "Keep pending text"})
        self.assertEqual(result["error"]["code"], "busy")
        self.assertEqual(len(self.host.ui_queue), 1)
        future = self.host.ui_queue[0][0]
        self.assertTrue(future.cancelled())
        self.host._run_ui_request(self.host.ui_queue[0])
        self.assertEqual(self.host.sends, 0)
        self.assertEqual(self.host.gate.snapshot()["activity_count"], 0)

    def test_running_future_with_pending_inner_and_late_revocation_has_no_effect(self):
        self.host.ui_mode = "running"
        original_authorized = self.host._require_authorized

        def authorized(peer, generation=None):
            if threading.current_thread().name == "owned-rpc-storage-ui":
                self.host.ui_entered.set()
                self.assertTrue(self.host.ui_finish.wait(3))
            return original_authorized(peer, generation)

        with patch.object(self.host, "_require_authorized", side_effect=authorized), patch.object(runtime, "Future", ShortFuture):
            result = self.host.rpc("chat.send", {"conversation_id": self.host.cid, "text": "Owned permission race"})
            self.assertEqual(result["error"]["code"], "busy")
            self.assertTrue(self.host.ui_queue[0][0].running())
            self.assertEqual(self.host.gate.snapshot()["activity_count"], 0)
            self.host.authorized = False
            self.host.ui_finish.set()
            for thread in self.host.ui_threads: thread.join(4)
        self.assertEqual(self.host.sends, 0)

    def test_storage_write_rechecks_revocation_after_reservation(self):
        reserve = self.host.sync.reserve_operation

        def revoked(*args, **kwargs):
            result = reserve(*args, **kwargs)
            self.host.authorized = False
            return result

        with patch.object(self.host.sync, "reserve_operation", side_effect=revoked):
            result = self.host.rpc("image.upload", {"filename": "owned.png", "data_base64": "QUJD"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "unauthorized")
        self.assertEqual(self.host.images.uploads, 0)

    def test_delete_queries_fail_closed_and_do_not_reserve(self):
        def failed_query():
            raise OSError("PRIVATE_QUERY_PATH_CANARY")

        for value in (True, None, 0, "False", failed_query):
            with self.subTest(value=type(value).__name__):
                query = value if callable(value) else lambda: value
                request = str(uuid4())
                with patch.object(self.host.chat._agent_service, "execution_resources_pending", side_effect=query):
                    result = self.host.rpc("image.delete", {"id": IMAGE}, request)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], "busy")
                self.assertNotIn("PRIVATE_QUERY_PATH_CANARY", str(result))
                self.assertIsNone(self.host.sync.get_operation(PEER, request))
        with patch.object(self.host.queue, "has_image_maintenance_blocker", side_effect=failed_query):
            self.assertEqual(self.host.rpc("image.delete", {"id": IMAGE})["error"]["code"], "busy")
        del self.host.chat._agent_service
        self.assertEqual(self.host.rpc("image.delete", {"id": IMAGE})["error"]["code"], "busy")
        self.assertEqual(self.host.images.deletes, 0)

    def test_held_fake_delete_keeps_maintenance_and_readonly_receipts_available(self):
        request = str(uuid4())
        results = []

        def hold():
            self.host.ui_entered.set()
            self.assertTrue(self.host.ui_finish.wait(3))

        self.host.images.on_delete = hold
        thread = threading.Thread(target=lambda: results.append(self.host.rpc("image.delete", {"id": IMAGE}, request)),
                                  name="owned-rpc-storage-delete")
        self.host.ui_threads.append(thread)
        thread.start()
        try:
            self.assertTrue(self.host.ui_entered.wait(3))
            self.assertTrue(self.host.gate.snapshot()["maintenance_active"])
            self.assertEqual(self.host.rpc("operation.get", {"request_id": request})["result"]["state"], "pending")
            self.assertTrue(self.host.rpc("image.list")["ok"])
            self.assertTrue(self.host.rpc("image.get", {"id": IMAGE})["ok"])
            self.assertEqual(self.host.rpc("image.upload", {"data_base64": "QUJD"})["error"]["code"], "busy")
        finally:
            self.host.ui_finish.set()
            thread.join(4)
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(self.host.images.deletes, 1)
        self.assertEqual(self.host.deleted_marks, [IMAGE])
        self.assertTrue(self.host.rpc("image.delete", {"id": IMAGE}, request)["result"]["duplicate"])
        self.assertEqual(self.host.images.deletes, 1)
        self.assertEqual(self.host.deleted_marks, [IMAGE])

    def test_duplicate_and_conflicting_payload_keep_existing_hash_binding(self):
        request = str(uuid4())
        params = {"filename": "owned.png", "data_base64": "QUJD"}
        self.assertTrue(self.host.rpc("image.upload", params, request)["ok"])
        maintenance = self.host.gate.maintenance(self.host.gate.create_owner())
        try:
            self.assertEqual(self.host.rpc("image.upload", params, request)["error"]["code"], "busy")
            self.assertEqual(self.host.rpc("operation.get", {"request_id": request})["result"]["state"], "complete")
        finally:
            self.host.gate.release(maintenance)
        self.assertTrue(self.host.rpc("image.upload", params, request)["result"]["duplicate"])
        conflict = self.host.rpc("image.upload", {**params, "data_base64": "REVG"}, request)
        self.assertFalse(conflict["ok"], conflict)
        self.assertEqual(self.host.images.uploads, 1)

    def test_receipt_failure_releases_activity_without_repeating_upload(self):
        request = str(uuid4())

        def failed_receipt(*args, **kwargs):
            self.assert_maintenance_blocked()
            raise OSError("PRIVATE_RECEIPT_PATH_CANARY")

        with patch.object(self.host.sync, "complete_operation", side_effect=failed_receipt):
            result = self.host.rpc("image.upload", {"filename": "owned.png", "data_base64": "QUJD"}, request)
        self.assertFalse(result["ok"], result)
        self.assertNotIn("PRIVATE_RECEIPT_PATH_CANARY", str(result))
        self.assertEqual(self.host.images.uploads, 1)
        self.assertEqual(self.host.sync.get_operation(PEER, request)["state"], "pending")
        self.assertEqual(self.host.gate.snapshot()["activity_count"], 0)

    def test_display_notification_failure_does_not_replace_completed_receipt(self):
        with patch.object(self.host.chat, "notify_image_storage_changed", side_effect=RuntimeError("OWNED_CLOSED_DISPLAY")):
            result = self.host.rpc("image.upload", {"filename": "owned.png", "data_base64": "QUJD"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.host.images.uploads, 1)

    def test_real_queue_and_staged_activity_each_refuse_fake_deletion(self):
        reference = HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "d" * 64)
        job = self.host.queue.enqueue(reference)
        request = str(uuid4())
        result = self.host.rpc("image.delete", {"id": IMAGE}, request)
        self.assertEqual(result["error"]["code"], "busy")
        self.assertIsNone(self.host.sync.get_operation(PEER, request))
        self.host.queue.request_cancel(job.job_id)
        owner = self.host.gate.create_owner()
        imported = self.host.gate.activity(owner, stage_on_success=True)
        self.host.gate.run(imported, lambda: None)
        staged = self.host.gate.handoff_to_stage(imported)
        try:
            self.assertEqual(self.host.rpc("image.delete", {"id": IMAGE})["error"]["code"], "busy")
            self.assertEqual(self.host.images.deletes, 0)
        finally:
            self.host.gate.release(staged)
        self.assertTrue(self.host.rpc("image.delete", {"id": IMAGE})["ok"])
        self.assertEqual(self.host.images.deletes, 1)

    def test_generation_change_after_reservation_cannot_upload(self):
        reserve = self.host.sync.reserve_operation

        def restarted(*args, **kwargs):
            result = reserve(*args, **kwargs)
            self.host._generation += 1
            return result

        with patch.object(self.host.sync, "reserve_operation", side_effect=restarted):
            result = self.host.rpc("image.upload", {"filename": "owned.png", "data_base64": "QUJD"})
        self.assertEqual(result["error"]["code"], "disabled")
        self.assertEqual(self.host.images.uploads, 0)

    def test_missing_delete_marker_refuses_before_image_delete(self):
        del self.host.chat.mark_image_storage_deleted
        request = str(uuid4())
        result = self.host.rpc("image.delete", {"id": IMAGE}, request)
        self.assertEqual(result["error"]["code"], "busy")
        self.assertEqual(self.host.images.deletes, 0)
        self.assertIsNone(self.host.sync.get_operation(PEER, request))

    def test_upload_invalidates_epoch_before_receipt_and_activity_release(self):
        complete = self.host.sync.complete_operation

        def receipt(*args, **kwargs):
            self.assertEqual(self.host.chat.image_storage_epoch, 1)
            self.assertEqual(self.host.mutated_marks, [IMAGE])
            self.assert_maintenance_blocked()
            return complete(*args, **kwargs)

        request = str(uuid4())
        params = {"filename": "owned.png", "data_base64": "QUJD"}
        with patch.object(self.host.sync, "complete_operation", side_effect=receipt):
            self.assertTrue(self.host.rpc("image.upload", params, request)["ok"])
        self.assertEqual(self.host.gate.snapshot()["activity_count"], 0)
        self.assertTrue(self.host.rpc("image.upload", params, request)["result"]["duplicate"])
        self.assertEqual(self.host.mutated_marks, [IMAGE])
        self.assertTrue(self.host.rpc("image.upload", params)["ok"])
        self.assertEqual(self.host.mutated_marks, [IMAGE, IMAGE])

    def test_failed_delete_invalidates_before_receipt_and_preserves_original_error(self):
        def delete_failure():
            raise RemoteError("storage_error", "OWNED_DELETE_FAILED")

        original_mark = self.host.chat.mark_image_storage_deleted
        complete = self.host.sync.complete_operation

        def mark(image_id):
            self.assertTrue(self.host.gate.snapshot()["maintenance_active"])
            original_mark(image_id)
            raise RuntimeError("PRIVATE_MARK_FAILURE_CANARY")

        def receipt(*args, **kwargs):
            self.assertTrue(self.host.gate.snapshot()["maintenance_active"])
            self.assertEqual(self.host.chat.image_storage_epoch, 1)
            return complete(*args, **kwargs)

        self.host.images.on_delete = delete_failure
        with patch.object(self.host.chat, "mark_image_storage_deleted", side_effect=mark), \
                patch.object(self.host.sync, "complete_operation", side_effect=receipt):
            result = self.host.rpc("image.delete", {"id": IMAGE})
        self.assertEqual(result["error"], {"code": "storage_error", "message": "OWNED_DELETE_FAILED"})
        self.assertEqual(self.host.deleted_marks, [IMAGE])
        self.assertFalse(self.host.gate.snapshot()["maintenance_active"])

    def test_snapshot_upload_invalidates_epoch_and_missing_marker_refuses(self):
        observer, settings = object(), object()
        with patch.object(self.host, "_desktop_capture_permission", return_value=(observer, settings)), \
                patch.object(runtime, "capture_desktop_image", return_value={"data_base64": "QUJD"}):
            self.assertTrue(self.host.rpc("desktop.snapshot")["ok"])
        self.assertEqual(self.host.chat.image_storage_epoch, 1)
        self.assertEqual(self.host.mutated_marks, [IMAGE])
        del self.host.chat.mark_image_storage_mutated
        request = str(uuid4())
        self.assertEqual(self.host.rpc("image.upload", {"data_base64": "QUJD"}, request)["error"]["code"], "busy")
        self.assertIsNone(self.host.sync.get_operation(PEER, request))
        self.assertEqual(self.host.images.uploads, 1)


if __name__ == "__main__":
    unittest.main()
