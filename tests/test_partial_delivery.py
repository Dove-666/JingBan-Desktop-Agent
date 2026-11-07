"""Interrupted delivery proves individual owned files, never whole-task success."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from goal_contract import GoalContract
from data_tools import DataTools
from partial_delivery import (FailureDiagnostic, FailureRecorder, model_request_failure,
                              partial_delivery, stop_failure, PARTIAL_SCOPE)
from task_harness import TaskHarness
from desktop_pet.agents.artifacts import ArtifactStore


class PartialDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-partial-")
        self.root = Path(self.owned.name)
        self.harness = TaskHarness(self.root, goal_contract=GoalContract.from_task(
            'Create report.txt containing exactly "HELLO".'))
        self.diagnostic = FailureDiagnostic("sdk_turn", "SDK_TURN_FAILED_UNKNOWN_CAUSE")

    def tearDown(self):
        self.owned.cleanup()

    def write(self, path="report.txt", text="HELLO"):
        args = {"path": path, "content": text}
        value = self.harness.files.write(**args)
        self.harness.record_success("write_file", args, value)

    def deliver(self, **kwargs):
        return partial_delivery(self.harness, self.diagnostic,
            authorization_current=kwargs.pop("authorization_current", lambda: True), **kwargs)

    def test_actual_write_is_partial_with_missing_whole_goal_judge(self):
        self.write()
        value = self.deliver()
        self.assertEqual(value["status"], "needs_user")
        self.assertFalse(value["acceptance"]["passed"])
        self.assertEqual(value["reason_code"], "SDK_TURN_FAILED_UNKNOWN_CAUSE")
        self.assertEqual(value["artifacts"][0]["scope"], PARTIAL_SCOPE)
        self.assertEqual(value["artifacts"][0]["path"], "report.txt")
        self.assertIn("整体任务尚未完成", value["reply"])

    def test_no_write_or_verify_only_input_cannot_be_delivered(self):
        (self.root / "report.txt").write_text("HELLO", encoding="utf-8")
        self.assertIsNone(self.deliver())
        self.harness.verify_artifact("report.txt")
        self.assertIsNone(self.deliver())

    def test_changed_version_bad_format_and_failed_exact_requirement_are_rejected(self):
        self.write()
        (self.root / "report.txt").write_text("CHANGED", encoding="utf-8")
        self.assertIsNone(self.deliver())
        self.harness = TaskHarness(self.root, goal_contract=GoalContract.from_task(
            'Create report.txt containing exactly "HELLO".'))
        (self.root / "report.txt").unlink()
        self.write(text="WRONG")
        self.assertIsNone(self.deliver())
        self.write("invalid.json", "not JSON")
        self.assertIsNone(self.deliver())

    def test_good_file_can_be_retained_when_another_file_is_invalid(self):
        self.write()
        self.write("invalid.json", "not JSON")
        self.assertEqual([item["path"] for item in self.deliver()["artifacts"]], ["report.txt"])

    def test_source_derived_partial_output_is_rejected_after_input_changes(self):
        self.harness = TaskHarness(self.root)
        source = self.root / "source.csv"
        source.write_text("category,amount\na,1.10\nb,2.20\n", encoding="utf-8")
        args = {"path": "source.csv", "value_column": "amount", "group_by": "category"}
        result = DataTools(self.root).analyze_csv(**args)
        self.harness.record_success("analyze_csv", args, result)
        self.write("summary.json", json.dumps({"category_totals": {"a": "1.1", "b": "2.2"},
                                               "total": "3.3", "record_count": 2}))
        self.assertIsNotNone(self.deliver())
        source.write_text("category,amount\na,99.00\nb,2.20\n", encoding="utf-8")
        self.assertIsNone(self.deliver())

    def test_unknown_effect_gui_and_unfinished_plan_remain_blockers(self):
        self.write()
        self.harness.update_plan([{"content": "Still pending", "status": "pending"}])
        self.harness.begin_gui_action()
        before = deepcopy(self.harness.plan)
        value = self.deliver(unknown_effects=True)
        self.assertEqual(value["status"], "needs_user")
        self.assertFalse(value["acceptance"]["passed"])
        self.assertIn("GOAL_EFFECT_UNRESOLVED", value["acceptance"]["issue_codes"])
        self.assertEqual(self.harness.plan, before)
        self.assertFalse(self.harness.gui_goal_verified)

    def test_revoked_authority_cancel_and_recheck_fail_closed(self):
        self.write()
        self.assertIsNone(self.deliver(authorization_current=lambda: False))
        self.assertIsNone(self.deliver(authorization_current=lambda: 1))
        checks = iter((True, True, True, False))
        self.assertIsNone(self.deliver(authorization_current=lambda: next(checks)))
        self.assertIsNone(partial_delivery(self.harness, stop_failure("cancelled"), authorization_current=lambda: True))

    def test_host_delivery_rechecks_hash_authority_and_conversation(self):
        self.write()
        report = self.deliver()["artifacts"][0]
        store = ArtifactStore(self.root / "deliveries")
        cid = str(uuid4())
        item = store.register(cid, report, self.harness.files)
        self.assertEqual(item["verification_scope"], PARTIAL_SCOPE)
        with self.assertRaisesRegex(ValueError, "ARTIFACT_UNAVAILABLE"):
            store.get(str(uuid4()), item["reference"])
        (self.root / "report.txt").write_text("CHANGED", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ARTIFACT_CHANGED"):
            store.register(cid, report, self.harness.files)
        with patch.object(self.harness.files, "read_bytes", side_effect=ValueError("ACCESS_DENIED")):
            with self.assertRaisesRegex(ValueError, "ACCESS_DENIED"):
                store.register(cid, report, self.harness.files)


class FailureDiagnosticTests(unittest.TestCase):
    def test_only_structured_status_and_whitelisted_codes_are_used(self):
        class Hostile:
            error_status_code = 401
            def __str__(self): raise AssertionError("raw exception string read")
            @property
            def body(self): raise AssertionError("provider body read")
            @property
            def content(self): raise AssertionError("provider content read")
        self.assertEqual(model_request_failure(Hostile()).code, "MODEL_REQUEST_AUTH_FAILED")
        for status, code in ((429, "MODEL_REQUEST_RATE_LIMITED"), (503, "MODEL_REQUEST_UNAVAILABLE")):
            value = type("Metadata", (), {"status_code": status})()
            self.assertEqual(model_request_failure(value).code, code)
        self.assertEqual(model_request_failure(TimeoutError()).code, "MODEL_REQUEST_TIMEOUT")
        value = type("Metadata", (), {"error_code": "context_length_exceeded"})()
        self.assertEqual(model_request_failure(value).code, "MODEL_REQUEST_CONTEXT_LIMIT")
        self.assertEqual(stop_failure("SECRET arbitrary stop reason").code, "EXECUTION_STOP_REASON_UNKNOWN")

    def test_recovered_model_failure_cannot_be_blamed_for_later_sdk_error(self):
        recorder = FailureRecorder()
        first = recorder.begin_model_request()
        recorder.model_failed(first, type("Metadata", (), {"status_code": 401})())
        self.assertEqual(recorder.turn_failed().code, "MODEL_REQUEST_AUTH_FAILED")
        second = recorder.begin_model_request()
        recorder.model_succeeded(second)
        recorder.model_failed(first)
        self.assertEqual(recorder.turn_failed().code, "SDK_TURN_FAILED_UNKNOWN_CAUSE")


if __name__ == "__main__":
    unittest.main()
