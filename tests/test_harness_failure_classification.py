"""Optional no-effect failures cannot erase actual required outcome checks."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"))
from goal_contract import GoalContract
from task_harness import TaskHarness


class HarnessFailureClassificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-failure-classification-")
        self.root = Path(self.directory.name)
        (self.root / "report.txt").write_text("DONE", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def harness(self, *, pdf_required=False):
        requirements = [{"path": "report.txt", "exact_text": "DONE"}]
        if pdf_required:
            requirements.append({"path": "report.pdf", "format": "pdf"})
        return TaskHarness(self.root, goal_contract=GoalContract("Deliver the host-specified report",
            requirements=requirements, require_judge=False))

    def test_pure_run_code_syntax_error_then_corrected_code_should_not_remain_required(self):
        harness = self.harness()
        harness.record_failure("run_code", {"code": "return (;"}, code="CODE_EXECUTION_FAILED")
        harness.record_success("run_code", {"code": "return 2 + 2;"}, '{"result":4}')
        self.assertTrue(harness.assess()["passed"])
        self.assertEqual(harness.failure_events[0]["category"], "exploratory")

    def test_host_confirmed_optional_no_effect_render_failure_allows_satisfied_content_goal(self):
        harness = self.harness()
        harness.record_failure("render_document", {"path": "report.txt"},
            classification="exploratory", code="OFFICE_UNSUPPORTED_FORMAT")
        self.assertTrue(harness.assess()["passed"])
        self.assertEqual(harness.failure_events[-1]["category"], "exploratory")

    def test_optional_render_failure_cannot_approve_missing_required_pdf(self):
        harness = self.harness(pdf_required=True)
        harness.record_failure("render_document", {"path": "report.txt"},
            classification="exploratory", code="OFFICE_UNSUPPORTED_FORMAT")
        assessment = harness.assess()
        self.assertFalse(assessment["passed"])
        self.assertIn("GOAL_REQUIREMENT_FAILED", assessment["issues"])

    def test_code_string_alone_does_not_authenticate_a_safe_render_failure(self):
        for code in ("OFFICE_RUNTIME_UNAVAILABLE", "OFFICE_PROTOCOL", "OFFICE_OWNERSHIP_UNCONFIRMED"):
            with self.subTest(code=code):
                harness = self.harness()
                harness.record_failure("render_document", {"path": "report.txt"}, code=code)
                self.assertIn("UNRESOLVED_TOOL_FAILURE", harness.assess()["issues"])

    def test_explicit_required_source_and_artifact_failure_still_block(self):
        for tool, arguments in (("read_file", {"path": "missing-source.txt"}),
                                ("verify_artifact", {"path": "report.txt"})):
            with self.subTest(tool=tool):
                harness = self.harness()
                harness.record_failure(tool, arguments, classification="required")
                harness.record_failure("render_document", {"path": "report.txt"}, classification="exploratory")
                self.assertIn("UNRESOLVED_TOOL_FAILURE", harness.assess()["issues"])

    def test_unknown_effect_and_gui_parent_gate_survive_optional_failure_policy(self):
        harness = self.harness()
        harness.record_failure("render_document", {"path": "report.txt"}, classification="uncertain")
        harness.record_failure("run_code", {"code": "return (;"}, classification="exploratory")
        harness.begin_gui_action()
        assessment = harness.assess()
        self.assertIn("UNRESOLVED_TOOL_FAILURE", assessment["issues"])
        self.assertIn("GUI_GOAL_UNVERIFIED", assessment["issues"])


if __name__ == "__main__":
    unittest.main()
