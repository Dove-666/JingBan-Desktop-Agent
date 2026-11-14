"""Persisted DSH cleanup must supplement, never replace, an owned Job receipt."""
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.agents.job_guardian import PrivateRunStore, new_manifest
from desktop_pet.agents.guardian_native import GuardianError


@unittest.skipUnless(sys.platform == "win32", "Windows private storage and DPAPI")
class DshGuardianCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dsh-guardian-cleanup-")
        self.addCleanup(self.tmp.cleanup)
        self.manifest = new_manifest(job_id=str(uuid4()), conversation_id=str(uuid4()),
            generation=str(uuid4()), operation_id=str(uuid4()), installation_id="a" * 64, launch_epoch="b" * 64,
            process_creation_time=1234)
        self.store = PrivateRunStore.create(Path(self.tmp.name), self.manifest)
        self.addCleanup(self.store.close)

    def write_job_receipt(self):
        self.store.write_closure(*self.store.identity(), trigger="stop")

    def test_restart_job_empty_cannot_prove_external_worker_closed(self):
        self.store.mark_external_cleanup_required()
        self.write_job_receipt()
        fresh = PrivateRunStore(self.store.path)
        try:
            self.assertFalse(fresh.external_cleanup_confirmed())
            self.assertIsNone(fresh.read_closure(expected_manifest=self.manifest))
            self.assertIsNone(fresh.consume_closure(expected_manifest=self.manifest))
            self.assertFalse((fresh.path / "consumed.json").exists())
            self.store.mark_external_cleanup_confirmed()
            self.assertTrue(fresh.external_cleanup_confirmed())
            self.assertIsNotNone(fresh.read_closure(expected_manifest=self.manifest))
        finally:
            fresh.close()

    def test_external_confirmation_alone_is_not_a_job_receipt(self):
        self.store.mark_external_cleanup_required()
        self.store.mark_external_cleanup_confirmed()
        self.assertIsNone(self.store.read_closure(expected_manifest=self.manifest))

    def test_malformed_external_record_fails_closed(self):
        self.store.mark_external_cleanup_required()
        self.write_job_receipt()
        self.store._write_new("external-closed.json", b'{"state":"closed"}')
        self.assertIsNone(self.store.read_closure(expected_manifest=self.manifest))

    def test_historical_in_job_only_receipt_still_recovers(self):
        self.write_job_receipt()
        self.assertIsNotNone(self.store.read_closure(expected_manifest=self.manifest))
        with self.assertRaises(GuardianError):
            self.store.mark_external_cleanup_confirmed()


if __name__ == "__main__": unittest.main()
