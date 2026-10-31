"""Image type validation, bounded storage, and failed-transaction recovery."""

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from desktop_pet.remote.attachments import ImageInbox
from desktop_pet.remote.errors import RemoteError
from tests.test_remote_controller import image_params


class RemoteImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-remote-images-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.inbox = ImageInbox(self.path)

    def test_invalid_file_disguised_as_image_is_rejected(self):
        params = image_params()
        params["data_base64"] = base64.b64encode(b"not an image").decode()
        with self.assertRaises(RemoteError):
            self.inbox.upload(params)
        self.assertEqual(self.inbox.list()["items"], [])

    def test_mismatched_mime_and_hash_are_rejected(self):
        for extra in ({"mime_type": "image/jpeg"}, {"sha256": "f" * 64}):
            with self.subTest(extra=extra), self.assertRaises(RemoteError):
                self.inbox.upload({**image_params(), **extra})

    def test_quota_refuses_without_evicting_existing_images(self):
        first = self.inbox.upload(image_params())
        # Deduplication does not count the same content twice.
        with patch("desktop_pet.remote.attachments.MAX_TOTAL_BYTES", 0):
            self.assertEqual(self.inbox.upload(image_params())["id"], first["id"])
        self.assertEqual(len(self.inbox.list()["items"]), 1)

    def test_failed_insert_does_not_leave_untracked_file(self):
        db = self.inbox._db()
        db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON images BEGIN SELECT RAISE(ABORT,'test'); END")
        db.commit()
        db.close()
        with self.assertRaises(Exception):
            self.inbox.upload(image_params())
        self.assertEqual(list(self.path.glob("*.png")), [])
        self.assertEqual(list(self.path.glob("*.tmp")), [])
        self.assertEqual(self.inbox.list()["items"], [])

    def test_failed_delete_restores_image_and_row(self):
        image = self.inbox.upload(image_params())
        db = self.inbox._db()
        db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON images BEGIN SELECT RAISE(ABORT,'test'); END")
        db.commit()
        db.close()
        with self.assertRaises(Exception):
            self.inbox.delete(image["id"])
        self.assertEqual(self.inbox.get(image["id"])["sha256"], image["sha256"])

    def test_restart_cleans_orphan_files_and_recovers_interrupted_delete(self):
        image = self.inbox.upload(image_params())
        original = self.path / (image["id"] + ".png")
        original.rename(original.with_name(original.name + ".deleting"))
        orphan = self.path / ("e" * 64 + ".png")
        orphan.write_bytes(b"uncommitted image")
        restarted = ImageInbox(self.path)
        self.assertEqual(len(restarted.list()["items"]), 1)
        self.assertFalse(orphan.exists())
        self.assertEqual(restarted.get(image["id"])["size"], image["size"])


if __name__ == "__main__":
    unittest.main()
