"""Independent publication/Stop receipt review; no native or model code runs."""
import asyncio
import json
import unittest
from unittest.mock import patch

from agent_backend.nanobot import project_snapshot as project
from tests import test_project_snapshot as fixtures


class ProjectPublicationIndependentReviewTests(unittest.TestCase):
    setUp = fixtures.ProjectSnapshotTests.setUp
    source = fixtures.ProjectSnapshotTests.source
    guard = fixtures.ProjectSnapshotTests.guard
    plan = fixtures.ProjectSnapshotTests.plan
    transport = fixtures.ProjectSnapshotTests.transport
    execute = fixtures.ProjectSnapshotTests.execute

    def test_cancellation_after_real_commit_retains_uncertainty_and_never_writes_twice(self):
        plan = self.plan(); self.execute(plan)
        write = self.files.write_bytes
        cancelled = asyncio.CancelledError("OWNED_PRIVATE_CANCEL_DETAIL")
        calls = []
        def commit_then_cancel(*args, **kwargs):
            calls.append(True); write(*args, **kwargs); raise cancelled
        with patch.object(self.files, "write_bytes", side_effect=commit_then_cancel):
            with self.assertRaises(asyncio.CancelledError) as raised: plan.publish()
            self.assertIs(cancelled, raised.exception)
            self.assertEqual("unknown", raised.exception.effect_state)
            self.assertTrue(raised.exception.publication_attempted and plan.publication_attempted)
            with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"): plan.publish()
        self.assertEqual(1, len(calls))
        self.assertEqual(self.artifact, (self.workspace/"result.json").read_bytes())
        self.assertIsNone(plan._receipt)
        self.assertIsNone(plan._captured)

    def test_incorrect_receipt_after_real_commit_is_unknown_and_disallows_second_publish(self):
        plan = self.plan(); self.execute(plan)
        write = self.files.write_bytes
        def incorrect_receipt(*args, **kwargs):
            value = json.loads(write(*args, **kwargs)); value["artifact"]["sha256"] = "0"*64
            return json.dumps(value)
        with patch.object(self.files, "write_bytes", side_effect=incorrect_receipt) as call:
            with self.assertRaises(project.ProjectPublicationUnknown): plan.publish()
            with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"): plan.publish()
            self.assertEqual(1, call.call_count)
        self.assertEqual(self.artifact, (self.workspace/"result.json").read_bytes())

    def test_nested_unicode_target_is_published_as_the_exact_relative_path(self):
        target = "reports/鲸🐳/result.json"
        plan = self.plan(output=project.ProjectOutput("build/result.json", target))
        self.execute(plan)
        receipt = plan.publish()
        self.assertEqual(target, receipt["artifact"]["path"])
        self.assertEqual(self.artifact, (self.workspace/target).read_bytes())
        self.assertFalse(receipt["task_verified"])


if __name__ == "__main__": unittest.main()
