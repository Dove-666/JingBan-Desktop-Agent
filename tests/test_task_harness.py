"""Adversarial completion gates over real owned files, no model or GUI."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from data_tools import DataTools
from safe_files import WorkspaceFiles
from task_harness import HarnessError, TaskHarness, canonical_plan


class TaskHarnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-harness-")
        self.root = Path(self.directory.name)
        self.harness = TaskHarness(self.root)
        self.files = WorkspaceFiles(self.root)
        self.data = DataTools(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def wrote(self, path, content):
        args = {"path": path, "content": content, "overwrite": (self.root / path).exists()}
        result = self.files.write(**args)
        self.harness.record_success("write_file", args, result)

    def source(self, path="source.csv", *, delimiter=",", values=("10.10", "20.20")):
        text = delimiter.join(("类别", "金额")) + "\n"
        text += "\n".join(delimiter.join((name, value)) for name, value in zip(("a", "b"), values)) + "\n"
        (self.root / path).write_text(text, encoding="utf-8")
        return text

    def analyze(self, path="source.csv", *, delimiter=",", value_column="金额"):
        args = {"path": path, "value_column": value_column, "group_by": "类别", "delimiter": delimiter}
        result = self.data.analyze_csv(**args)
        self.harness.record_success("analyze_csv", args, result)
        return json.loads(result)

    def summary(self, path="summary.json", *, total="30.3", groups=None, count=2):
        data = {"category_totals": groups if groups is not None else {"a": "10.1", "b": "20.2"},
                "total": total, "record_count": count}
        self.wrote(path, json.dumps(data))
        return data

    def verify(self, path="summary.json", **options):
        args = {"path": path, **options}
        raw = self.harness.verify_artifact(**args)
        result = json.loads(raw)
        if result["passed"]:
            self.harness.record_success("verify_artifact", args, raw)
        else:
            self.harness.record_failure("verify_artifact", args)
        return result

    def assert_blocked(self):
        reply, status, assessment = self.harness.finalize("模型声称完成", "completed")
        self.assertEqual(status, "needs_user", assessment)
        self.assertFalse(assessment["passed"], assessment)
        return assessment

    def test_plan_normalizes_text_and_rejects_duplicates_parallel_or_bad_shape(self):
        plan = canonical_plan([{"content": "  read input  ", "status": "in_progress"},
                               {"content": "verify output", "status": "pending"}])
        self.assertEqual(plan[0]["content"], "read input")
        bad = [[], None, {}, [{"content": "", "status": "pending"}],
               [{"content": "x", "status": "unknown"}],
               [{"content": "x", "status": "pending", "extra": True}],
               [{"content": "x", "status": "pending"}, {"content": " x ", "status": "pending"}],
               [{"content": "x", "status": "in_progress"}, {"content": "y", "status": "in_progress"}]]
        # An empty whole plan is a valid initial state; other malformed values fail.
        self.assertEqual(canonical_plan(bad.pop(0)), [])
        for value in bad:
            with self.subTest(value=value), self.assertRaises(HarnessError):
                canonical_plan(value)

    def test_plan_snapshot_is_not_mutable_by_callers(self):
        supplied = [{"content": "read", "status": "pending"}]
        self.harness.update_plan(supplied)
        supplied[0]["status"] = "completed"
        assessment = self.harness.assess()
        assessment["plan"][0]["status"] = "completed"
        self.assertEqual(self.harness.plan[0]["status"], "pending")

    def test_nonstring_lifecycle_values_are_static_validation_errors(self):
        for status in ([], {}, True, None):
            with self.subTest(status=status), self.assertRaises(HarnessError):
                canonical_plan([{"content": "check output", "status": status}])
            with self.subTest(finish=status), self.assertRaises(HarnessError):
                self.harness.finish_task(status)

    def test_unfinished_plan_cannot_be_erased_by_empty_or_nonempty_replacement(self):
        self.harness.update_plan([{"content": "read material", "status": "in_progress"},
                                  {"content": "check result", "status": "pending"}])
        for steps in ([], [{"content": "unrelated done", "status": "completed"}],
                      [{"content": "read material", "status": "completed"}]):
            with self.subTest(steps=steps), self.assertRaises(HarnessError):
                self.harness.update_plan(steps)
        self.assert_blocked()

    def test_completed_plan_can_be_replaced_and_normal_qa_needs_no_fake_tools(self):
        self.harness.update_plan([{"content": "explain", "status": "completed"}])
        self.harness.update_plan([])
        reply, status, assessment = self.harness.finalize("普通问题的回答")
        self.assertEqual(status, "completed")
        self.assertTrue(assessment["passed"])

    def test_legacy_final_answer_cannot_bypass_bad_json_sum_without_finish_tool(self):
        self.summary(total="20.3")
        assessment = self.assert_blocked()
        self.assertIn("ARTIFACT_VERIFICATION_FAILED", assessment["issues"])

    def test_correct_text_is_read_back_without_claiming_arbitrary_semantic_success(self):
        self.wrote("result.txt", "owned output 中文🙂")
        reply, status, assessment = self.harness.finalize("已写入")
        self.assertEqual(status, "completed")
        self.assertTrue(assessment["passed"])
        self.assertFalse(assessment["artifacts"][0]["task_fully_verified"])
        self.assertEqual(assessment["artifacts"][0]["scope"], "file_readback")

    def test_changed_text_after_verification_invalidates_prior_write_and_completion(self):
        self.wrote("result.txt", "authorized original")
        self.assertTrue(self.verify("result.txt")["passed"])
        (self.root / "result.txt").write_text("unexpected external edit", encoding="utf-8")
        self.assert_blocked()

    def test_new_authorized_write_replaces_old_expected_hash_and_can_be_verified(self):
        self.wrote("result.txt", "first")
        self.assertTrue(self.verify("result.txt")["passed"])
        self.wrote("result.txt", "authorized second")
        self.assertTrue(self.verify("result.txt")["passed"])
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_deleted_artifact_cannot_reuse_a_previous_pass(self):
        self.wrote("result.txt", "owned text")
        self.assertTrue(self.verify("result.txt")["passed"])
        (self.root / "result.txt").unlink()
        self.assert_blocked()

    def test_bad_json_after_successful_verification_is_blocked_without_raising(self):
        self.summary()
        self.assertTrue(self.verify()["passed"])
        (self.root / "summary.json").write_text('{"total":NaN}', encoding="utf-8")
        self.assert_blocked()

    def test_verified_json_is_rechecked_after_another_model_write(self):
        self.source()
        self.analyze()
        self.summary()
        self.assertTrue(self.verify(kind="csv_summary", source_path="source.csv", value_column="金额", group_by="类别")["passed"])
        self.summary(total="3", groups={"a": "1", "b": "2"})
        self.assert_blocked()

    def test_explicit_weak_kind_cannot_downgrade_available_source_evidence(self):
        self.source()
        self.analyze()
        for kind in ("auto", "text", "json"):
            with self.subTest(kind=kind):
                self.summary(total="3", groups={"a": "1", "b": "2"})
                self.assertFalse(self.verify(kind=kind)["passed"])
                self.assert_blocked()

    def test_uppercase_json_extension_does_not_bypass_source_evidence(self):
        self.source()
        self.analyze()
        self.summary("REPORT.JSON", total="3", groups={"a": "1", "b": "2"})
        self.assert_blocked()

    def test_multiple_csv_sources_require_explicit_selection(self):
        self.source("first.csv")
        self.source("second.csv", values=("4", "5"))
        self.analyze("first.csv")
        self.analyze("second.csv")
        self.summary(total="3", groups={"a": "1", "b": "2"})
        assessment = self.assert_blocked()
        issues = {code for item in assessment["artifacts"] for code in item["issues"]}
        self.assertIn("SOURCE_SELECTION_REQUIRED", issues)

    def test_multiple_csv_sources_explicit_matching_source_is_accepted(self):
        self.source("first.csv")
        self.source("second.csv", values=("4", "5"))
        self.analyze("first.csv")
        self.analyze("second.csv")
        self.summary()
        result = self.verify(kind="csv_summary", source_path="first.csv", value_column="金额", group_by="类别")
        self.assertTrue(result["passed"], result)
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_ungrouped_summary_with_exact_source_total_is_accepted(self):
        self.source()
        args = {"path": "source.csv", "value_column": "金额"}
        raw = self.data.analyze_csv(**args)
        self.harness.record_success("analyze_csv", args, raw)
        self.summary(groups={})
        result = self.verify(kind="csv_summary", source_path="source.csv", value_column="金额")
        self.assertTrue(result["passed"], result)
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_one_csv_can_have_two_explicitly_selected_aggregate_configurations(self):
        (self.root / "source.csv").write_text("类别,金额,数量\na,10.10,1\nb,20.20,2\n", encoding="utf-8")
        self.analyze(value_column="金额")
        self.summary("money.json")
        self.analyze(value_column="数量")
        self.summary("count.json", total="3", groups={"a": "1", "b": "2"})
        first = self.verify("money.json", kind="csv_summary", source_path="source.csv", value_column="金额", group_by="类别")
        second = self.verify("count.json", kind="csv_summary", source_path="source.csv", value_column="数量", group_by="类别")
        self.assertTrue(first["passed"], first)
        self.assertTrue(second["passed"], second)
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_large_valid_summary_cannot_hide_source_mismatch_after_read_projection_limit(self):
        self.source()
        self.analyze()
        data = {"category_totals": {"a": "1", "b": "2"}, "total": "3", "record_count": 2,
                "padding": "x" * 30000}
        self.wrote("summary.json", json.dumps(data))
        self.assertFalse(self.verify(kind="auto")["passed"])
        self.assert_blocked()

    def test_invalid_declared_source_returns_static_failure_and_finalize_stays_safe(self):
        self.summary()
        result = self.verify(kind="csv_summary", source_path="../private.csv", value_column="金额")
        self.assertFalse(result["passed"])
        self.assertNotIn("../private.csv", json.dumps(result))
        self.assert_blocked()

    def test_semicolon_csv_uses_strong_check_even_for_auto_request(self):
        self.source(delimiter=";")
        self.analyze(delimiter=";")
        self.summary(total="3", groups={"a": "1", "b": "2"})
        self.assertFalse(self.verify(kind="auto")["passed"])
        self.summary()
        self.assertTrue(self.verify(kind="auto")["passed"])
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_csv_read_without_deterministic_analysis_is_not_source_proof(self):
        self.source()
        self.harness.record_success("read_file", {"path": "source.csv"}, self.files.read("source.csv"))
        self.summary()
        assessment = self.assert_blocked()
        self.assertTrue(any("SOURCE_ANALYSIS_REQUIRED" in item["issues"] for item in assessment["artifacts"]))

    def test_source_changed_after_analysis_invalidates_explicit_verified_summary(self):
        self.source()
        self.analyze()
        self.summary()
        self.assertTrue(self.verify(kind="csv_summary", source_path="source.csv", value_column="金额", group_by="类别")["passed"])
        # A byte-identity change invalidates the evidence even if sums stay equal.
        source = self.root / "source.csv"
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        assessment = self.assert_blocked()
        self.assertTrue(any("SOURCE_CHANGED_AFTER_ANALYSIS" in item["issues"] for item in assessment["artifacts"]))

    def test_direct_source_verification_keeps_its_source_identity_until_finalization(self):
        self.source()
        self.summary()
        self.assertTrue(self.verify(kind="csv_summary", source_path="source.csv", value_column="金额", group_by="类别")["passed"])
        source = self.root / "source.csv"
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assert_blocked()

    def test_deleted_or_malformed_csv_source_cannot_produce_completed_state(self):
        self.source()
        self.analyze()
        self.summary()
        self.assertTrue(self.verify(kind="csv_summary", source_path="source.csv", value_column="金额", group_by="类别")["passed"])
        (self.root / "source.csv").write_bytes(b"\xff\xfe")
        self.assert_blocked()
        (self.root / "source.csv").unlink()
        self.assert_blocked()

    def test_reanalysis_after_intentional_source_change_can_restore_valid_evidence(self):
        self.source()
        self.analyze()
        self.summary()
        self.source(values=("4", "5"))
        self.analyze()
        self.summary(total="9", groups={"a": "4", "b": "5"})
        self.assertTrue(self.verify(kind="csv_summary", source_path="source.csv", value_column="金额", group_by="类别")["passed"])
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_unrelated_success_does_not_wash_away_missing_material_error(self):
        self.harness.record_failure("read_file", {"path": "missing.txt"})
        self.wrote("other.txt", "other input")
        self.harness.record_success("read_file", {"path": "other.txt"}, self.files.read("other.txt"))
        self.harness.record_success("system_info", {}, "{}")
        self.assert_blocked()
        (self.root / "missing.txt").write_text("material supplied", encoding="utf-8")
        self.harness.record_success("read_file", {"path": "missing.txt"}, self.files.read("missing.txt"))
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_corrected_csv_column_on_same_source_resolves_failed_analysis(self):
        self.source()
        self.harness.record_failure("analyze_csv", {"path": "source.csv", "value_column": "wrong"})
        self.analyze(value_column="金额")
        self.summary()
        self.assertEqual(self.harness.finalize("done")[1], "completed")

    def test_finish_needs_user_is_not_overridden_by_later_plain_final_text(self):
        result = json.loads(self.harness.finish_task("needs_user", "请提供缺失的材料"))
        self.assertTrue(result["accepted"])
        self.assertEqual(self.harness.finalize("模型后来又说完成", "completed")[1], "needs_user")

    def test_completed_finish_rejection_is_still_enforced_by_legacy_finalize(self):
        self.harness.update_plan([{"content": "wait for input", "status": "pending"}])
        result = json.loads(self.harness.finish_task("completed", "done"))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "needs_user")
        self.assert_blocked()

    def test_gui_or_cancellation_fallback_status_is_never_upgraded(self):
        self.harness.finish_task("completed", "done")
        for status in ("needs_user", "cancelled", "failed"):
            with self.subTest(status=status):
                self.assertEqual(self.harness.finalize("text", status)[1], status)


if __name__ == "__main__":
    unittest.main()
