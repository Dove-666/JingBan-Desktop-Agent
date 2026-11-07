"""Deliver the approved answer itself; later attempts invalidate its seal."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_goal_contract_worker as support
from test_harness_worker import tool


class CompletionDeliveryTests(unittest.TestCase):
    run_case = support.GoalContractWorkerTests.run_case

    def prefix(self):
        return [tool("write_file", path="report.txt", content="HELLO"), tool("read_file", path="report.txt")]

    def test_approved_candidate_is_the_delivered_and_verified_answer(self):
        result, files, snapshots = self.run_case([*self.prefix(),
            tool("finish_task", status="completed", summary=support.DONE), "Unchecked later rewrite."])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reply"], support.DONE)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(files["report.txt"], "HELLO")

    def test_even_failed_read_after_acceptance_invalidates_delivery_seal(self):
        result, _, snapshots = self.run_case([*self.prefix(),
            tool("finish_task", status="completed", summary=support.DONE),
            tool("read_file", path="nonexistent-probe.txt"), "New final answer after another tool attempt."])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(result["reply"], "New final answer after another tool attempt.")

    def test_new_write_cannot_reuse_an_earlier_pass(self):
        result, files, snapshots = self.run_case([*self.prefix(),
            tool("finish_task", status="completed", summary=support.DONE),
            tool("write_file", path="report.txt", content="WRONG", overwrite=True), "Everything is correct."])
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(files["report.txt"], "WRONG")
        self.assertEqual(len(snapshots), 2)

    def test_wrong_format_candidate_is_not_reused_over_a_correct_final(self):
        original = support.SeparateGoalModel.judge
        def formatted(model, snapshot):
            receipt = json.loads(original(model, snapshot))
            if snapshot["answer"] != '{"ok":true}':
                receipt["status"] = "not_satisfied"
            return json.dumps(receipt)
        with patch.object(support.SeparateGoalModel, "judge", formatted):
            result, _, snapshots = self.run_case([*self.prefix(),
                tool("finish_task", status="completed", summary="An incorrectly formatted summary."),
                '{"ok":true}'], task=support.FILE_TASK + ' Return exactly JSON {"ok":true}.')
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reply"], '{"ok":true}')
        self.assertEqual(len(snapshots), 2)


if __name__ == "__main__":
    unittest.main()
