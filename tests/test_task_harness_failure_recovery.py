"""Recovery counters over actual owned observations, without providers or GUI."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from safe_files import WorkspaceFiles
from task_harness import TaskHarness
from goal_contract import GoalContract
from gui_verification import report


class TaskHarnessFailureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-failure-recovery-")
        self.root = Path(self.directory.name)
        self.harness = TaskHarness(self.root)
        self.files = WorkspaceFiles(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def repeated_failure(self, name, arguments, *, category="required", count=4):
        for _ in range(count):
            self.harness.record_failure(name, arguments, classification=category)

    def read_owned(self, name, text="actual fixture data"):
        (self.root / name).write_text(text, encoding="utf-8")
        arguments = {"path": name}
        self.harness.record_success("read_file", arguments, self.files.read(name))
        return arguments

    def test_observation_and_planning_success_do_not_hide_repeated_failed_subgoal(self):
        arguments = {"task": "Observe the owned fixture window without input"}
        for _ in range(4):
            self.harness.record_failure("computer_use", arguments, classification="exploratory")
            for tool in ("desktop_observe", "desktop_windows", "update_plan", "recall_context", "system_info"):
                self.harness.record_success(tool, {}, "{}")
        self.assertTrue(self.harness.stalled)

    def test_unrelated_executed_action_does_not_reset_required_failure_chain(self):
        arguments = {"ref": "owned-control", "property": "checked", "expected": True}
        self.repeated_failure("desktop_verify_state", arguments, count=3)
        self.harness.record_success("open_application", {"application_name": "Owned fixture"}, "{}")
        self.harness.record_failure("desktop_verify_state", arguments, classification="required")
        self.assertTrue(self.harness.stalled)
        self.assertIn(self.harness._operation_key("desktop_verify_state", arguments), self.harness.failed_tools)

    def test_matching_required_read_success_restores_progress_and_completion(self):
        arguments = {"path": "required.txt"}
        self.repeated_failure("read_file", arguments)
        self.assertTrue(self.harness.stalled)
        self.read_owned("required.txt")
        self.assertFalse(self.harness.stalled)
        self.assertFalse(self.harness.failure_counts)
        self.assertEqual(self.harness.finalize("The required input was read.")[1], "completed")

    def test_successful_alternative_read_clears_exploratory_candidate_failure(self):
        arguments = {"path": "missing-candidate.txt"}
        self.repeated_failure("read_file", arguments, category="exploratory")
        self.assertTrue(self.harness.stalled)
        self.read_owned("actual-supplied-input.txt")
        self.assertFalse(self.harness.stalled)
        self.harness.record_failure("read_file", arguments, classification="exploratory")
        self.assertFalse(self.harness.stalled)

    def test_successful_alternative_read_preserves_explicit_required_candidate(self):
        arguments = {"path": "required.txt"}
        self.repeated_failure("read_file", arguments)
        self.read_owned("unrelated.txt")
        self.assertTrue(self.harness.stalled)
        self.assertEqual(self.harness.finalize("Only the other file was read.")[1], "needs_user")
        self.read_owned("required.txt")
        self.assertFalse(self.harness.stalled)

    def test_matching_success_cannot_settle_unknown_input_without_host_observation(self):
        arguments = {"ref": "owned-like-control", "action": "invoke"}
        self.repeated_failure("desktop_act", arguments, category="uncertain")
        self.harness.record_success("desktop_act", arguments, '{"status":"executed"}')
        self.assertTrue(self.harness.stalled)
        self.assertIn(self.harness._operation_key("desktop_act", arguments), self.harness.failed_tools)
        self.harness.resolve_effects({"desktop_act"})
        self.assertFalse(self.harness.stalled)
        self.assertFalse(self.harness.failure_counts)

    def test_host_resolving_another_input_domain_does_not_clear_current_unknown(self):
        arguments = {"ref": "owned-control", "action": "invoke"}
        self.repeated_failure("desktop_act", arguments, category="uncertain")
        self.harness.resolve_effects({"computer_use"})
        self.assertTrue(self.harness.stalled)

    def test_host_verified_parent_goal_supersedes_failed_checks_and_unknown_inputs(self):
        self.harness.begin_gui_action()
        self.harness.record_failure("desktop_act", {"ref": "owned-control", "action": "invoke"},
                                    classification="uncertain")
        self.repeated_failure("verify_desktop_goal", {})
        self.harness.record_gui_verification(report("verified", reason="OWNED_FIXTURE_CONFIRMED"))
        self.assertTrue(self.harness.gui_goal_verified)
        self.assertFalse(self.harness.stalled)
        self.assertFalse(self.harness.failed_tools)

    def test_failed_readonly_desktop_view_does_not_become_a_required_effect(self):
        self.harness.record_failure("desktop_view", {"question": "Describe the owned fixture window"},
                                    code="SCREENSHOT_FAILED")
        self.assertEqual(self.harness.failure_events[-1]["category"], "exploratory")
        self.assertNotIn("UNRESOLVED_TOOL_FAILURE", self.harness.assess()["issues"])

    def test_calculation_recovery_preserves_explicit_and_uncertain_blockers(self):
        uncertain = {"expression": "owned-unknown-fixture"}
        required = {"expression": "owned-required-fixture"}
        self.harness.record_failure("calculate", required, classification="required")
        self.repeated_failure("calculate", uncertain, category="uncertain")
        self.harness.record_success("calculate", {"expression": "2+2"}, '{"value":"4"}')
        self.assertTrue(self.harness.stalled)
        for arguments in (required, uncertain):
            self.assertIn(self.harness._operation_key("calculate", arguments), self.harness.failed_tools)

    def test_successful_calculation_replaces_nonrequired_failed_expression(self):
        self.repeated_failure("calculate", {"expression": "1/0"}, category="exploratory")
        self.harness.record_success("calculate", {"expression": "2+2"}, '{"value":"4"}')
        self.assertFalse(self.harness.stalled)
        self.assertEqual(self.harness.finalize("4")[1], "completed")

    def test_repairing_the_same_file_resets_recheck_attempts_but_not_required_verification(self):
        arguments = {"path": "result.txt"}
        self.repeated_failure("verify_artifact", arguments)
        written = {"path": "result.txt", "content": "repaired fixture"}
        result = self.files.write(**written)
        self.harness.record_success("write_file", written, result)
        self.assertFalse(self.harness.stalled)
        self.assertIn(self.harness._operation_key("verify_artifact", arguments), self.harness.failed_tools)
        verified = self.harness.verify_artifact(**arguments)
        self.assertTrue(json.loads(verified)["passed"])
        self.harness.record_success("verify_artifact", arguments, verified)
        self.assertEqual(self.harness.finalize("The repaired output was read back.")[1], "completed")

    def test_many_successful_reads_do_not_introduce_a_task_step_limit(self):
        self.read_owned("actual-input.txt")
        for _ in range(150):
            self.harness.record_success("read_file", {"path": "actual-input.txt"}, self.files.read("actual-input.txt"))
        self.assertFalse(self.harness.stalled)

    def test_alternating_failed_candidates_stop_after_repeated_cycle(self):
        for index in range(6):
            self.harness.record_failure("read_file", {"path": f"missing-{index % 2}.txt"})
        self.assertFalse(self.harness.stalled)
        self.harness.record_failure("read_file", {"path": "missing-0.txt"})
        self.assertTrue(self.harness.stalled)
        self.read_owned("actual-input.txt")
        self.assertFalse(self.harness.stalled)

    def test_distinct_exploration_does_not_create_a_global_failure_limit(self):
        for index in range(150):
            self.harness.record_failure("read_file", {"path": f"candidate-{index}.txt"})
            self.assertFalse(self.harness.stalled)
        self.assertLessEqual(len(self.harness.failure_counts), 16)
        self.assertLessEqual(len(self.harness.failure_operations), 16)

    def test_unchanged_read_keeps_retry_progress_stable_but_invalidates_judge_receipt(self):
        self.harness = TaskHarness(self.root, goal_contract=GoalContract.from_task(
            'Create report.txt containing exactly "HELLO".'))
        self.read_owned("report.txt", "WRONG")
        progress = self.harness.completion_progress_id()
        evidence = self.harness.goal_snapshot()["evidence_id"]
        for _ in range(4):
            self.harness.record_failure("finish_task", {"progress": self.harness.completion_progress_id()},
                                        classification="exploratory")
            self.read_owned("report.txt", "WRONG")
            self.assertEqual(self.harness.completion_progress_id(), progress)
        self.assertTrue(self.harness.stalled)
        self.assertNotEqual(self.harness.goal_snapshot()["evidence_id"], evidence)

    def test_changed_file_and_revised_goal_are_actual_progress(self):
        self.harness = TaskHarness(self.root, goal_contract=GoalContract.from_task(
            'Create report.txt containing exactly "HELLO".'))
        self.read_owned("report.txt", "WRONG")
        before = self.harness.completion_progress_id()
        (self.root / "report.txt").write_text("HELLO", encoding="utf-8")
        after = self.harness.completion_progress_id()
        self.assertNotEqual(before, after, "Fresh file content must allow a new completion attempt")
        self.harness.goal_contract = GoalContract.from_task('Read report.txt.')
        self.assertNotEqual(after, self.harness.completion_progress_id())


if __name__ == "__main__":
    unittest.main()
