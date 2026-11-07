"""Independent local-draft storage boundaries, using only temporary files."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet import draft_store as subject


class DraftStoreIndependentReviewTests(unittest.TestCase):
    def test_oversized_existing_database_is_rejected_before_sqlite_parses_it(self):
        with tempfile.TemporaryDirectory(prefix="aura-draft-oversized-review-") as temporary:
            path = Path(temporary)/"owned.drafts.sqlite3"
            with path.open("wb") as stream: stream.truncate(subject.MAX_DATABASE_BYTES+1)
            store = subject.LocalDraftStore(path)
            with patch.object(subject.sqlite3, "connect", side_effect=AssertionError("Must not parse oversized input")) as connect:
                with self.assertRaises(subject.DraftStorageError): store.load()
                connect.assert_not_called()

    def test_repeated_epoch_rotation_keeps_old_edits_rejected_after_new_store_instance(self):
        with tempfile.TemporaryDirectory(prefix="aura-draft-epoch-review-") as temporary:
            path = Path(temporary)/"owned.drafts.sqlite3"
            cid = str(uuid4()); store = subject.LocalDraftStore(path)
            old = subject.new_draft(cid, "OLD_UNSENT")
            self.assertTrue(store.save(old))
            middle = subject.new_draft(cid, "")
            self.assertTrue(store.rotate(middle, expected_epoch=old.epoch))
            fresh = subject.new_draft(cid, "NEW_UNSENT")
            self.assertTrue(store.rotate(fresh, expected_epoch=middle.epoch))
            restarted = subject.LocalDraftStore(path)
            self.assertFalse(restarted.save(old))
            self.assertFalse(restarted.save(middle))
            self.assertEqual(fresh, restarted.lookup(cid))


if __name__ == "__main__": unittest.main()
