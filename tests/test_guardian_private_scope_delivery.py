"""Temporary host grant-delivery policy; no workers, models or real App data."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from uuid import uuid4

from desktop_pet.agents.service import AgentService
from desktop_pet.agents.contracts import AgentError


class GuardianPrivateScopeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="guardian-grant-owned-")
        self.root = Path(self.temporary.name)
        self.config, self.workspace = self.root / "appconfig", self.root / "workspace"
        self.config.mkdir(); self.workspace.mkdir()
        self.private = self.config / "host_job_guardian"; self.private.mkdir()
        self.cid, self.task = str(uuid4()), "owned-fixed-task"
        self.pipe = io.BytesIO()
        self.authorization_thread = None
        self.service = AgentService(checkpoint_path=self.config / "checkpoints.db")
        self.service._active_control = {"conversation_id": self.cid, "scope": "a" * 64, "task_id": self.task,
            "workspace": str(self.workspace), "cancel": threading.Event(), "process": SimpleNamespace(stdin=self.pipe)}

    def tearDown(self):
        # A failed liveness check must not make unittest enter the same lock
        # forever. This daemon owns only this temporary fake-stdio service;
        # no worker, child process, model or real App state exists.
        if self.authorization_thread is None or not self.authorization_thread.is_alive():
            self.service.shutdown()
        self.temporary.cleanup()

    def request(self, path, *, directory=True):
        identity = "fixed-file-access"
        self.service._interaction_broker.open({"id": identity, "kind": "file_access", "question": "owned fixed grant",
            "resource": {"path": str(path), "access": "read", "directory": directory}},
            conversation_id=self.cid, task_id=self.task)
        replies, errors = [], []
        def authorize():
            try:
                replies.append(self.service.answer_interaction(self.cid, identity, {"allowed": True}))
            except BaseException as error:
                errors.append(error)
        self.authorization_thread = threading.Thread(target=authorize, daemon=True)
        self.authorization_thread.start()
        self.authorization_thread.join(1)
        self.assertFalse(self.authorization_thread.is_alive(),
            "Host grant authorization must settle without recursively acquiring a non-reentrant lock")
        if errors:
            raise errors[0]
        self.assertEqual(replies, [True])
        return json.loads(self.pipe.getvalue())

    def test_authorized_appconfig_ancestor_is_denied_before_grant_cache_or_worker_delivery(self):
        frame = self.request(self.config)
        self.assertFalse(frame["answer"]["allowed"])
        self.assertNotIn("grant", frame["answer"])
        self.assertEqual(self.service._file_grants, {})

    def test_private_record_file_is_not_read_or_delivered_as_grant(self):
        target = self.private / "owned-record.txt"; target.write_text("fixed synthetic record")
        frame = self.request(target, directory=False)
        self.assertFalse(frame["answer"]["allowed"])
        self.assertNotIn("grant", frame["answer"])
        self.assertNotIn("fixed synthetic record", self.pipe.getvalue().decode())

    def test_unrelated_authorized_directory_preserves_existing_grant_behavior(self):
        target = self.root / "allowed"; target.mkdir()
        frame = self.request(target)
        self.assertTrue(frame["answer"]["allowed"])
        self.assertEqual(frame["answer"]["grant"]["path"], str(target.resolve()))
        self.assertEqual(len(self.service._file_grants[(self.cid, "a" * 64)]), 1)

    def test_old_grant_from_another_conversation_is_included_in_fresh_exclusion(self):
        self.service._file_grants[(str(uuid4()), "b" * 64)] = [
            {"path": str(self.config), "access": "read", "directory": True}]
        with self.assertRaises(AgentError) as caught:
            self.service._check_guardian_scope(self.workspace)
        self.assertEqual(caught.exception.code, "guardian_scope_conflict")


if __name__ == "__main__": unittest.main()
