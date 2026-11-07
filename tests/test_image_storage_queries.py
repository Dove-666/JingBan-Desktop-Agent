"""Owned SQLite/image fixtures for the not-yet-integrated maintenance queries."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from desktop_pet.agents.job_queue import (
    HostDispatchScope, HostJobReference, JobQueue, JobQueueError, STATES, WorkerStoppedProof,
)
from desktop_pet.remote.attachments import ImageInbox, MAX_TOTAL_BYTES
from desktop_pet.remote.errors import RemoteError


PRIVATE = "SYNTHETIC_SECRET_PRIVATE_PATH_DO_NOT_EXPOSE"


def image_params(index=0):
    image = QImage(24, 16, QImage.Format.Format_ARGB32)
    image.fill(0xff000000 | index)
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            raise AssertionError("OWNED_IMAGE_ENCODING_FAILED")
    finally:
        buffer.close()
    return {"filename": "owned.png", "mime_type": "image/png",
            "data_base64": base64.b64encode(bytes(encoded)).decode("ascii")}


class ImageUsageQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-image-usage-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = ImageInbox(self.root / "inbox")
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.worker.shutdown, wait=True)

    def query(self):
        return self.worker.submit(self.inbox.usage).result(timeout=5)

    def test_empty_total_has_fixed_limit_and_counts(self):
        self.assertEqual(self.query(), {"count": 0, "used_bytes": 0, "limit_bytes": 64 * 1024 * 1024})
        self.assertEqual(MAX_TOTAL_BYTES, 64 * 1024 * 1024)

    def test_totals_cover_more_than_maximum_list_page_without_loading_images(self):
        rows = [self.inbox.upload(image_params(index)) for index in range(101)]
        page = self.inbox.list(limit=100)
        self.assertEqual(len(page["items"]), 100)
        self.assertTrue(page["has_more"])
        with patch.object(self.inbox, "list", side_effect=AssertionError("NO_PAGED_USAGE")), \
             patch.object(self.inbox, "get", side_effect=AssertionError("NO_IMAGE_BYTES")):
            usage = self.query()
        self.assertEqual(usage, {"count": 101, "used_bytes": sum(row["size"] for row in rows),
                                 "limit_bytes": MAX_TOTAL_BYTES})

    def test_dedup_and_delete_change_logical_usage_exactly_once(self):
        params = image_params(12)
        first = self.inbox.upload(params)
        self.assertEqual(self.inbox.upload({**params, "filename": "same-bytes.png"})["id"], first["id"])
        second = self.inbox.upload(image_params(13))
        self.assertEqual(self.query()["count"], 2)
        self.assertEqual(self.query()["used_bytes"], first["size"] + second["size"])
        self.inbox.delete(first["id"])
        self.inbox.delete(first["id"])
        self.assertEqual(self.query(), {"count": 1, "used_bytes": second["size"], "limit_bytes": MAX_TOTAL_BYTES})
        self.inbox.delete(second["id"])
        self.assertEqual(self.query()["used_bytes"], 0)

    def test_usage_query_is_not_a_cached_previous_total(self):
        first = self.inbox.upload(image_params(32))
        self.assertEqual(self.query()["used_bytes"], first["size"])
        other_handle = ImageInbox(self.inbox.directory)
        second = other_handle.upload(image_params(33))
        self.assertEqual(self.query()["used_bytes"], first["size"] + second["size"])
        other_handle.delete(first["id"])
        self.assertEqual(self.query()["used_bytes"], second["size"])

    def test_query_failure_closes_actual_connection_and_does_not_return_empty(self):
        self.inbox._db().close()
        db = sqlite3.connect(self.inbox.directory / "images.sqlite3", check_same_thread=False)
        db.execute("DROP TABLE images")
        db.commit()
        with patch.object(self.inbox, "_db", return_value=db):
            with self.assertRaises(RemoteError) as caught:
                self.query()
        self.assertEqual(caught.exception.code, "storage_error")
        self.assertNotIn("images", str(caught.exception))
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_sqlite_and_local_io_errors_have_static_safe_error(self):
        with patch("desktop_pet.remote.attachments.sqlite3.connect",
                   side_effect=sqlite3.OperationalError(PRIVATE)):
            with self.assertRaises(RemoteError) as sqlite_error:
                self.query()
        self.assertEqual(sqlite_error.exception.code, "storage_error")
        self.assertNotIn(PRIVATE, str(sqlite_error.exception))
        blocked = self.root / PRIVATE
        blocked.write_bytes(b"owned-path-blocker")
        self.inbox = ImageInbox(blocked)
        with self.assertRaises(RemoteError) as io_error:
            self.query()
        self.assertEqual(io_error.exception.code, "storage_error")
        self.assertNotIn(PRIVATE, str(io_error.exception))

    def test_corrupt_negative_or_noninteger_totals_are_unavailable(self):
        image = self.inbox.upload(image_params())
        for value in (-1, "invalid-size"):
            with self.subTest(value_type=type(value).__name__):
                with closing(sqlite3.connect(self.inbox.directory / "images.sqlite3")) as db:
                    with db:
                        db.execute("UPDATE images SET size=? WHERE id=?", (value, image["id"]))
                with self.assertRaises(RemoteError) as caught:
                    self.query()
                self.assertEqual(caught.exception.code, "storage_error")


class ImageQueueBlockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-image-queue-blocker-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.queue = JobQueue(self.path, clock=lambda: 1000)
        self.queue.initialize()
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.worker.shutdown, wait=True)

    @staticmethod
    def reference():
        return HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64)

    @staticmethod
    def stopped(job):
        return WorkerStoppedProof(job.job_id, job.conversation_id, job.generation, job.user_message_id, True)

    @staticmethod
    def revalidate(job):
        return HostDispatchScope(job.conversation_id, job.generation, job.settings_scope, True)

    def query(self):
        return self.worker.submit(self.queue.has_image_maintenance_blocker).result(timeout=5)

    def seed_terminal_history(self, count=505):
        rows = [(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64)
                for _ in range(count)]
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.executemany("""INSERT INTO jobs(job_id,conversation_id,generation,user_message_id,
                    idempotency_key,settings_scope,state,created_at,updated_at,worker_closed_at,finished_at)
                    VALUES(?,?,?,?,?,?,'cancelled',1,1,1,1)""", rows)

    def test_empty_queue_does_not_block(self):
        self.assertIs(self.query(), False)

    def test_tail_queued_job_blocks_beyond_maximum_history_page(self):
        self.seed_terminal_history()
        tail = self.queue.enqueue(self.reference())
        page = self.queue.list_jobs(limit=500)
        self.assertEqual(len(page), 500)
        self.assertTrue(all(job.worker_closed_at is not None for job in page))
        self.assertNotIn(tail.job_id, {job.job_id for job in page})
        with patch.object(self.queue, "list_jobs", side_effect=AssertionError("NO_PAGED_BLOCKER")):
            self.assertIs(self.query(), True)
        self.queue.request_cancel(tail.job_id)
        self.assertIs(self.query(), False)

    def test_tail_unknown_and_interrupted_require_real_closure_proof(self):
        self.seed_terminal_history()
        for started in (False, True):
            with self.subTest(started=started):
                tail = self.queue.enqueue(self.reference())
                claimed = self.queue.claim()
                self.assertEqual(claimed.job.job_id, tail.job_id)
                if started:
                    self.assertTrue(self.queue.begin_dispatch(claimed.lease, revalidate=self.revalidate).accepted)
                self.queue.recover_after_restart()
                recovered = self.queue.get(tail.job_id)
                self.assertEqual(recovered.state, "unknown" if started else "interrupted")
                self.assertIsNone(recovered.worker_closed_at)
                self.assertIs(self.query(), True)
                closed = self.queue.reconcile_worker_stopped(tail.job_id, verifier=self.stopped)
                self.assertIsNotNone(closed.worker_closed_at)
                self.assertIs(self.query(), False)

    def test_every_state_with_missing_closure_blocks_and_only_pending_closed_states_block(self):
        job = self.queue.enqueue(self.reference())
        pending = {"queued", "claimed", "running", "awaiting_user", "cancel_requested"}
        for state in sorted(STATES):
            for closure in (None, 1000):
                with self.subTest(state=state, closed=closure is not None):
                    with closing(sqlite3.connect(self.path)) as db:
                        with db:
                            db.execute("UPDATE jobs SET state=?,worker_closed_at=? WHERE job_id=?",
                                       (state, closure, job.job_id))
                    self.assertIs(self.query(), state in pending or closure is None)

    def test_query_does_not_recover_cancel_dispatch_or_mutate_rows(self):
        job = self.queue.enqueue(self.reference())
        before = self.path.read_bytes()
        with patch.object(self.queue, "_recover", side_effect=AssertionError("NO_RECOVERY")):
            self.assertIs(self.query(), True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.queue.get(job.job_id).state, "queued")

    def test_missing_table_is_fixed_storage_error_not_false(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("DROP TABLE jobs")
        with self.assertRaises(JobQueueError) as caught:
            self.query()
        self.assertEqual(str(caught.exception), "QUEUE_STORAGE_UNAVAILABLE")

    def test_uninitialized_and_unavailable_stores_do_not_claim_idle(self):
        untouched = JobQueue(Path(self.temp.name) / "not-created.sqlite3")
        with self.assertRaises(JobQueueError) as unopened:
            untouched.has_image_maintenance_blocker()
        self.assertEqual(str(unopened.exception), "QUEUE_NOT_INITIALIZED")
        with patch("desktop_pet.agents.job_queue.sqlite3.connect",
                   side_effect=sqlite3.OperationalError(PRIVATE)):
            with self.assertRaises(JobQueueError) as caught:
                self.query()
        self.assertEqual(str(caught.exception), "QUEUE_STORAGE_UNAVAILABLE")
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.path.unlink()
        with self.assertRaises(JobQueueError) as missing:
            self.query()
        self.assertEqual(str(missing.exception), "QUEUE_STORAGE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
