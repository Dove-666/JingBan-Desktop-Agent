"""Independent composition review using synthetic preferences and queue only.

The callback below models required host revalidation. It is not product wiring,
does not start a worker and cannot prove dispatch/revoke integration is live.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import job_queue as queue
from desktop_pet.remote.errors import RemoteError
from desktop_pet.remote.settings import RemotePreferences


class RemoteAuthorityReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-authority-review-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = SimpleNamespace(config_path=self.root / "synthetic.ini", _credentials=None)
        self.preferences = RemotePreferences(self.settings)
        self.peer = "a" * 64
        self.preferences.add_device(self.peer, "Synthetic phone", "synthetic-time")
        self.preferences.set_enabled(True)
        self.path = self.root / "queue.sqlite3"
        self.ledger = queue.JobQueue(self.path, clock=lambda: 1000)
        self.ledger.initialize()

    def scope(self, peer):
        # Deliberately read current host authority, never the queue's old hash.
        grant = self.preferences.agent_authorization(peer)
        encoded = json.dumps({"settings": "synthetic-host-policy", "peer": peer,
                              "pairing_incarnation": grant}, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def reference(self, **changes):
        values = dict(conversation_id=str(uuid4()), generation=str(uuid4()),
                      user_message_id=str(uuid4()), idempotency_key=str(uuid4()),
                      settings_scope=self.scope(self.peer), origin_peer_id=self.peer)
        values.update(changes)
        return queue.HostJobReference(**values)

    def fresh_scope(self, job):
        peer = job.origin_peer_id
        allowed = (peer is not None and self.preferences.enabled and self.preferences.authorized(peer)
                   and self.preferences.agent_authorization(peer) is not None)
        return queue.HostDispatchScope(job.conversation_id, job.generation, self.scope(peer) if peer else "0" * 64,
                                       allowed, peer)

    def claim(self, reference=None):
        job = self.ledger.enqueue(reference or self.reference())
        claimed = self.ledger.claim()
        self.assertEqual(job.job_id, claimed.job.job_id)
        return job, claimed.lease

    def test_nonce_and_derived_authority_never_enter_public_projection_or_job_storage(self):
        raw = json.loads(self.preferences.path.read_text(encoding="utf-8"))
        nonce = raw["devices"][0]["agent_grant"]
        grant = self.preferences.agent_authorization(self.peer)
        public = self.preferences.devices
        self.assertEqual(set(public[0]), {"id", "name", "paired_at"})
        self.assertNotIn(nonce, json.dumps(public)); self.assertNotIn(grant, json.dumps(public))
        self.assertNotEqual(nonce, grant)
        self.ledger.enqueue(self.reference())
        with sqlite3.connect(self.path) as db:
            encoded = "\n".join(db.iterdump())
        self.assertNotIn(nonce, encoded)
        self.assertNotIn(grant, encoded)
        public[0]["name"] = "Changed outside projection"
        self.assertEqual("Synthetic phone", self.preferences.devices[0]["name"])

    def test_revoked_peer_is_blocked_even_with_original_origin_and_valid_lease(self):
        job, lease = self.claim()
        self.preferences.revoke(self.peer)
        decision = self.ledger.begin_dispatch(lease, revalidate=self.fresh_scope)
        self.assertFalse(decision.accepted)
        self.assertEqual("QUEUE_SCOPE_RECHECK_FAILED", decision.code)
        self.assertEqual(job.origin_peer_id, decision.job.origin_peer_id)

    def test_same_peer_repair_cannot_revive_job_from_previous_pairing_incarnation(self):
        reference = self.reference(); job, lease = self.claim(reference)
        original_grant = self.preferences.agent_authorization(self.peer)
        self.preferences.revoke(self.peer)
        self.preferences.add_device(self.peer, "Synthetic phone", "synthetic-time")
        self.assertNotEqual(original_grant, self.preferences.agent_authorization(self.peer))
        # Same idempotency key returns its old scope, rather than enrolling it
        # in the new pairing incarnation.
        retry = self.ledger.enqueue(replace(reference, settings_scope=self.scope(self.peer)))
        self.assertEqual(job.job_id, retry.job_id)
        self.assertEqual(reference.settings_scope, retry.settings_scope)
        decision = self.ledger.begin_dispatch(lease, revalidate=self.fresh_scope)
        self.assertFalse(decision.accepted)
        self.assertEqual("QUEUE_SETTINGS_CHANGED", decision.code)

    def test_disable_is_not_execution_permission_despite_persistent_pair_identity(self):
        job, lease = self.claim(); grant = self.preferences.agent_authorization(self.peer)
        self.preferences.set_enabled(False)
        self.assertEqual(grant, self.preferences.agent_authorization(self.peer))
        decision = self.ledger.begin_dispatch(lease, revalidate=self.fresh_scope)
        self.assertFalse(decision.accepted)
        self.assertEqual("blocked", decision.job.state)

    def test_metadata_change_and_reload_keep_identity_and_fresh_dispatch_works(self):
        job, lease = self.claim(); grant = self.preferences.agent_authorization(self.peer)
        self.preferences.add_device(self.peer, "Renamed phone", "different-public-time")
        self.preferences = RemotePreferences(self.settings)
        self.assertEqual(grant, self.preferences.agent_authorization(self.peer))
        self.assertTrue(self.ledger.begin_dispatch(lease, revalidate=self.fresh_scope).accepted)

    def test_legacy_read_paths_do_not_enroll_or_write_until_explicit_host_create(self):
        raw = json.loads(self.preferences.path.read_text(encoding="utf-8"))
        raw["devices"][0].pop("agent_grant")
        self.preferences.path.write_text(json.dumps(raw), encoding="utf-8")
        self.preferences = RemotePreferences(self.settings)
        before = self.preferences.path.read_bytes()
        for _ in range(3):
            self.assertTrue(self.preferences.enabled)
            self.assertTrue(self.preferences.authorized(self.peer))
            self.assertIsNone(self.preferences.agent_authorization(self.peer))
            self.assertEqual(set(self.preferences.devices[0]), {"id", "name", "paired_at"})
        self.assertEqual(before, self.preferences.path.read_bytes())
        with patch.object(self.preferences, "_commit", side_effect=RemoteError("storage_error", "Synthetic failure")):
            with self.assertRaises(RemoteError): self.preferences.agent_authorization(self.peer, create=True)
        self.assertIsNone(self.preferences.agent_authorization(self.peer))
        self.assertEqual(before, self.preferences.path.read_bytes())
        grant = self.preferences.agent_authorization(self.peer, create=True)
        self.assertEqual(grant, RemotePreferences(self.settings).agent_authorization(self.peer))

    def test_idempotent_origin_cannot_be_erased_changed_or_adopted_after_terminal(self):
        reference = self.reference(); job = self.ledger.enqueue(reference)
        self.ledger.request_cancel(job.job_id)
        for origin in (None, "b" * 64):
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_IDEMPOTENCY_CONFLICT"):
                self.ledger.enqueue(replace(reference, origin_peer_id=origin))
        self.assertEqual("cancelled", self.ledger.enqueue(reference).state)
        self.assertEqual(self.peer, self.ledger.get(job.job_id).origin_peer_id)

    def test_migrated_v1_reference_cannot_be_reinterpreted_as_current_remote_peer(self):
        reference = self.reference(origin_peer_id=None)
        job = self.ledger.enqueue(reference)
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE jobs DROP COLUMN origin_peer_id")
            db.execute("PRAGMA user_version=1")
        upgraded = queue.JobQueue(self.path, clock=lambda: 1000); upgraded.initialize()
        migrated = upgraded.get(job.job_id)
        self.assertEqual("queued", migrated.state)
        self.assertIsNone(migrated.origin_peer_id)
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_IDEMPOTENCY_CONFLICT"):
            upgraded.enqueue(replace(reference, origin_peer_id=self.peer))
        lease = upgraded.claim().lease
        decision = upgraded.begin_dispatch(lease, revalidate=lambda row: queue.HostDispatchScope(
            row.conversation_id, row.generation, row.settings_scope, True, self.peer))
        self.assertFalse(decision.accepted)
        self.assertEqual("QUEUE_SCOPE_RECHECK_FAILED", decision.code)


if __name__ == "__main__":
    unittest.main()
