"""Original-goal acceptance on owned fixtures, without a provider or desktop."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from goal_contract import GoalContract, GoalContractError, infer_file_requirements
from goal_evidence import GoalEvidence
from safe_files import WorkspaceFiles
from task_harness import TaskHarness


def verdict(snapshot, status="satisfied", ids=None):
    return {"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
            "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
            "status": status, "evidence_ids": list(snapshot["evidence_ids"] if ids is None else ids)}


class GoalContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-goal-contract-")
        self.root = Path(self.directory.name)
        self.files = WorkspaceFiles(self.root)
        self.evidence = GoalEvidence()

    def tearDown(self):
        self.directory.cleanup()

    def snapshot(self, contract, answer="Done", **kwargs):
        return contract.snapshot(self.files, self.evidence, answer=answer, **kwargs)

    def write(self, path="report.txt", text="HELLO"):
        args = {"path": path, "content": text}
        result = self.files.write(**args)
        self.evidence.observe("write_file", args, result)
        return result

    def test_file_creation_is_guarded_in_english_and_chinese(self):
        cases = (("Create report.txt containing exactly \"HELLO\".", "report.txt", "HELLO"),
                 ("Create report.txt containing exactly HELLO. Read it back.", "report.txt", "HELLO"),
                 ("请创建report.txt，内容为“你好”", "report.txt", "你好"),
                 ("帮我新建`我的报告.txt`，内容为`你好`", "我的报告.txt", "你好"),
                 ("把内容写入 report.txt", "report.txt", None))
        for task, path, exact in cases:
            with self.subTest(task=task):
                contract = GoalContract.from_task(task)
                self.assertEqual(contract.requirements[0]["kind"], "artifact")
                self.assertEqual(contract.requirements[0]["path"], path)
                if exact is not None:
                    self.assertEqual(contract.requirements[0]["exact_text"], exact)
                snapshot = self.snapshot(contract)
                report = contract.assess(snapshot, verdict(snapshot))
                self.assertIn("GOAL_REQUIREMENT_FAILED", report["issues"])

    def test_read_original_does_not_impose_a_creation(self):
        for task in ("Read the original report.txt", "读取report.txt原文", "显示 report.txt 的内容"):
            with self.subTest(task=task):
                items = infer_file_requirements(task)
                self.assertTrue(items)
                self.assertTrue(all(item["kind"] == "read_file" for item in items))
        self.write(text="Original contents")
        # A read-only task is not contaminated by a write from fixture setup.
        self.evidence = GoalEvidence()
        contract = GoalContract.from_task("读取report.txt原文")
        snapshot = self.snapshot(contract, answer="Original contents")
        self.assertTrue(contract.assess(snapshot, verdict(snapshot, "answer_only"))["passed"])

    def test_quotes_negation_translation_and_explanations_are_not_write_tasks(self):
        for task in ("Do not create report.txt.", "Don't create report.txt", "不要创建report.txt",
                     "解释如何创建 report.txt", "Explain how to create report.txt", "Translate \"Create report.txt\"",
                     "“创建report.txt”这段话是什么意思", '"Create report.txt" is just quoted text',
                     'Read the quotation "Create report.txt"',
                     "Here is an example: `Create report.txt`", "```\nCreate report.txt\n```\nExplain the example"):
            with self.subTest(task=task):
                self.assertFalse(any(item["kind"] == "artifact" for item in infer_file_requirements(task)))

    def test_input_files_are_not_misclassified_as_created_outputs(self):
        items = infer_file_requirements("Read source.csv and create output.json from input.csv")
        self.assertEqual([(item["kind"], item["path"]) for item in items],
                         [("read_file", "source.csv"), ("artifact", "output.json")])

    def test_unrelated_real_tool_cannot_satisfy_missing_output(self):
        contract = GoalContract.from_task("Create report.txt containing exactly \"HELLO\".")
        self.evidence.observe("system_info", {}, {"platform": "Windows", "processors": 8})
        snapshot = self.snapshot(contract, "Created report.txt with HELLO.")
        # Even an overly optimistic semantic judge cannot override file checks.
        report = contract.assess(snapshot, verdict(snapshot))
        self.assertFalse(report["passed"])
        self.assertIn("GOAL_REQUIREMENT_FAILED", report["issues"])
        self.assertEqual(snapshot["facts"]["files"], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_clipped_large_write_retains_versioned_receipt_without_claiming_full_readback(self):
        text = "鲸鱼" * 40000
        self.write(text=text)
        contract = GoalContract.from_task("Create report.txt", requirements=[
            {"kind": "artifact", "path": "report.txt", "write_observed": True}])
        snapshot = self.snapshot(contract)
        record = snapshot["facts"]["observed"]["observations"][0]
        self.assertTrue(record["truncated"])
        self.assertNotIn("observation", record)
        self.assertEqual(record["file_write"]["bytes"], len(text.encode("utf-8")))
        self.assertTrue(snapshot["facts"]["requirement_checks"][0]["passed"])
        self.assertTrue(snapshot["facts"]["files"][0]["truncated"])
        self.assertEqual(contract.assess(snapshot)["judge_status"], "missing")
        # A later external replacement cannot inherit the successful receipt.
        (self.root / "report.txt").write_text("external replacement", encoding="utf-8")
        changed = self.snapshot(contract)
        self.assertIn("GOAL_WRITE_NOT_OBSERVED", changed["facts"]["requirement_checks"][0]["issues"])

    def test_read_tool_or_planner_path_cannot_manufacture_a_write_receipt(self):
        raw = b"HELLO"
        import hashlib
        (self.root / "report.txt").write_bytes(raw)
        forged = {"written": "report.txt", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        self.evidence.observe("read_file", {"path": "report.txt"}, forged)
        self.evidence.observe("write_file", {"path": "report.txt", "content": "HELLO"}, {"claimed": forged})
        contract = GoalContract.from_task("Create report.txt", requirements=[
            {"kind": "artifact", "path": "report.txt", "write_observed": True}])
        snapshot = self.snapshot(contract)
        self.assertTrue(all("file_write" not in row for row in snapshot["facts"]["observed"]["observations"]))
        self.assertIn("GOAL_WRITE_NOT_OBSERVED", snapshot["facts"]["requirement_checks"][0]["issues"])

    def test_undeclared_actual_required_artifact_is_checked_without_tool_write(self):
        contract = GoalContract.from_task("Create report.txt containing exactly \"HELLO\".")
        (self.root / "report.txt").write_text("HELLO", encoding="utf-8")
        snapshot = self.snapshot(contract)
        self.assertTrue(contract.assess(snapshot, verdict(snapshot))["passed"])
        self.assertEqual(snapshot["facts"]["files"][0]["text"], "HELLO")
        self.assertEqual(snapshot["facts"]["files"][0]["source"], "host_file_readback")

    def test_mismatching_text_is_not_replaced_by_tool_or_finish_claims(self):
        self.write(text="WRONG")
        contract = GoalContract.from_task("Create report.txt containing exactly \"HELLO\".")
        snapshot = self.snapshot(contract, "I wrote HELLO and read it back.")
        report = contract.assess(snapshot, verdict(snapshot))
        self.assertFalse(report["passed"])
        self.assertIn("GOAL_FILE_TEXT_MISMATCH", report["requirements"][0]["issues"])
        self.assertEqual(snapshot["facts"]["files"][0]["text"], "WRONG")

    def test_host_exact_text_contains_format_and_strict_number_requirements(self):
        self.write("summary.json", '{"total":30.30,"groups":{"a/b":"0.1"},"title":"Report"}')
        contract = GoalContract("Build the host-defined report", require_judge=False, requirements=[{
            "kind": "artifact", "path": "summary.json", "format": "json", "contains_text": ["Report"],
            "json_values": [{"pointer": "/total", "number": Decimal("30.3")},
                            {"pointer": "/groups/a~1b", "number": "0.1"}], "write_observed": True}])
        self.assertTrue(contract.assess(self.snapshot(contract))["passed"])
        for contents in ('{"total":30.31,"groups":{"a/b":"0.1"},"title":"Report"}',
                         '{"total":NaN,"groups":{"a/b":"0.1"},"title":"Report"}',
                         '{"total":30.3,"total":30.3,"groups":{"a/b":"0.1"},"title":"Report"}',
                         '{"total":true,"groups":{"a/b":"0.1"},"title":"Report"}', "not JSON"):
            with self.subTest(contents=contents):
                (self.root / "summary.json").write_text(contents, encoding="utf-8")
                report = contract.assess(self.snapshot(contract))
                self.assertFalse(report["passed"])

    def test_host_requirement_errors_are_static(self):
        for requirement in ({"path": "a.txt", "provider_error": "SECRET"},
                            {"kind": "execute_shell", "command": "SECRET"},
                            {"path": "a.json", "json_values": [{"pointer": "/n", "number": float("nan")}]},
                            {"kind": "read_file", "path": "a.txt", "write_observed": True}):
            with self.subTest(requirement=requirement), self.assertRaises(GoalContractError) as caught:
                GoalContract("task", requirements=[requirement])
            self.assertNotIn("SECRET", str(caught.exception))

    def test_empty_exact_file_is_a_valid_explicit_requirement(self):
        self.write(text="")
        contract = GoalContract("Create an empty file", require_judge=False,
                                requirements=[{"path": "report.txt", "exact_text": ""}])
        self.assertTrue(contract.assess(self.snapshot(contract))["passed"])

    def test_read_only_question_can_truthfully_report_an_existing_empty_text_file(self):
        (self.root / "empty.txt").write_text("", encoding="utf-8")
        contract = GoalContract.from_task("Read empty.txt and tell me its contents")
        snapshot = self.snapshot(contract, "The file is empty.")
        self.assertTrue(contract.assess(snapshot, verdict(snapshot, "answer_only"))["passed"])

    def test_all_host_requirements_are_checked_even_if_model_omits_one_artifact(self):
        self.write("a.txt", "A")
        contract = GoalContract("Deliver both reports", require_judge=False,
            requirements=[{"path": "a.txt", "exact_text": "A"}, {"path": "b.txt", "exact_text": "B"}])
        report = contract.assess(self.snapshot(contract))
        self.assertFalse(report["passed"])
        self.assertTrue(report["requirements"][0]["passed"])
        self.assertFalse(report["requirements"][1]["passed"])

    def test_multiple_checks_for_one_file_share_real_readback(self):
        self.write()
        contract = GoalContract("Check all constraints", require_judge=False,
            requirements=[{"path": "report.txt", "exact_text": "HELLO"},
                          {"path": "report.txt", "contains_text": ["ELL"]}])
        snapshot = self.snapshot(contract)
        self.assertEqual(len(snapshot["facts"]["files"]), 1)
        self.assertTrue(contract.assess(snapshot)["passed"])

    def test_unknown_goal_actual_artifact_contents_are_independently_read_back(self):
        result = json.loads(self.write("actual.txt", "Actual data, not a summary claim"))
        contract = GoalContract.from_task("完成这个工作")
        snapshot = self.snapshot(contract, artifacts=[{"path": "actual.txt", "passed": True,
                                                       "sha256": result["sha256"]}])
        self.assertEqual(snapshot["facts"]["files"][0]["text"], "Actual data, not a summary claim")
        self.assertTrue(contract.assess(snapshot, verdict(snapshot))["passed"])
        stale = self.snapshot(contract, artifacts=[{"path": "actual.txt", "passed": True, "sha256": "0" * 64}])
        self.assertIn("GOAL_ARTIFACT_CHANGED", contract.assess(stale, verdict(stale))["issues"])

    def test_known_provider_secret_is_not_copied_from_file_into_judge_facts(self):
        self.evidence = GoalEvidence(secrets="TOP_SECRET")
        result = json.loads(self.write("actual.txt", "A value: TOP_SECRET"))
        contract = GoalContract.from_task("完成这个工作")
        snapshot = self.snapshot(contract, artifacts=[{"path": "actual.txt", "sha256": result["sha256"]}])
        self.assertNotIn("TOP_SECRET", json.dumps(snapshot["facts"]))
        self.assertIn("[redacted]", snapshot["facts"]["files"][0]["text"])

    def test_mutated_snapshot_cannot_reuse_a_valid_receipt(self):
        self.write()
        contract = GoalContract.from_task("Create report.txt")
        snapshot = self.snapshot(contract)
        receipt = verdict(snapshot)
        snapshot["facts"]["files"][0]["text"] = "A substituted fabricated proof"
        report = contract.assess(snapshot, receipt)
        self.assertEqual(report["issues"], ["GOAL_SNAPSHOT_INVALID"])

    def test_contract_cannot_grant_access_outside_current_file_capability(self):
        outside = self.root.parent / "aura-owned-goal-unauthorized.txt"
        contract = GoalContract("Read an unauthorized path", require_judge=False,
                                requirements=[{"kind": "read_file", "path": str(outside)}])
        report = contract.assess(self.snapshot(contract))
        self.assertFalse(report["passed"])
        self.assertEqual(report["requirements"][0]["issues"], ["GOAL_FILE_UNAVAILABLE"])
        with self.assertRaises(ValueError):
            self.files.resolve(str(outside))

    def test_unknown_goal_and_missing_or_illegal_judge_fail_closed(self):
        contract = GoalContract.from_task("整理一下这些资料")
        self.evidence.observe("system_info", {}, "Actual host state")
        snapshot = self.snapshot(contract)
        self.assertIn("GOAL_JUDGE_MISSING", contract.assess(snapshot)["issues"])
        for bad in ("provider SECRET failure", {}, {**verdict(snapshot), "reason": "SECRET"},
                    {**verdict(snapshot), "status": "completed"},
                    {**verdict(snapshot), "evidence_ids": ["invented-file-proof"]},
                    {**verdict(snapshot), "evidence_ids": []}):
            with self.subTest(bad=bad):
                report = contract.assess(snapshot, bad)
                self.assertFalse(report["passed"])
                self.assertNotIn("SECRET", json.dumps(report))
        self.assertFalse(contract.assess(snapshot, verdict(snapshot, "unknown"))["passed"])
        self.assertFalse(contract.assess(snapshot, verdict(snapshot, "not_satisfied"))["passed"])

    def test_judge_receipt_binds_original_task_real_observations_and_answer(self):
        self.write()
        contract = GoalContract.from_task("Create report.txt")
        snapshot = self.snapshot(contract)
        receipt = verdict(snapshot)
        self.assertTrue(contract.assess(snapshot, receipt)["passed"])
        self.assertIn("GOAL_JUDGE_STALE", contract.assess(self.snapshot(contract, "Different answer"), receipt)["issues"])
        self.evidence.observe("read_file", {"path": "report.txt"}, "HELLO")
        self.assertIn("GOAL_JUDGE_STALE", contract.assess(self.snapshot(contract), receipt)["issues"])
        other = GoalContract.from_task("Create report.txt then do another thing")
        self.assertIn("GOAL_JUDGE_STALE", other.assess(self.snapshot(other), receipt)["issues"])

    def test_file_change_after_verification_invalidates_receipt(self):
        self.write()
        contract = GoalContract.from_task("Create report.txt")
        snapshot = self.snapshot(contract)
        receipt = verdict(snapshot)
        (self.root / "report.txt").write_text("LATER VERSION", encoding="utf-8")
        self.assertIn("GOAL_JUDGE_STALE", contract.assess(self.snapshot(contract), receipt)["issues"])

    def test_answer_only_needs_no_unrelated_tool_but_does_need_independent_judge(self):
        contract = GoalContract.from_task("你好，今天可以聊聊吗")
        snapshot = self.snapshot(contract, "你好，可以的。")
        self.assertEqual(snapshot["evidence_ids"], [])
        self.assertTrue(contract.assess(snapshot, verdict(snapshot, "answer_only"))["passed"])
        self.assertFalse(contract.assess(snapshot)["passed"])
        self.assertFalse(contract.assess(snapshot, verdict(snapshot))["passed"])
        self.assertFalse(contract.assess(self.snapshot(contract, ""), verdict(self.snapshot(contract, ""), "answer_only"))["passed"])

    def test_answer_only_cannot_hide_required_outputs_effects_or_gui(self):
        contract = GoalContract.from_task("Create report.txt")
        self.write()
        snapshot = self.snapshot(contract)
        self.assertIn("GOAL_ANSWER_ONLY_INVALID", contract.assess(snapshot, verdict(snapshot, "answer_only"))["issues"])
        conversational = GoalContract.from_task("你好")
        snapshot = self.snapshot(conversational)
        self.assertFalse(conversational.assess(snapshot, verdict(snapshot, "answer_only"))["passed"])
        gui = GoalContract("Perform the operation", requirements=[{"kind": "gui"}])
        snapshot = self.snapshot(gui)
        report = gui.assess(snapshot, verdict(snapshot))
        self.assertFalse(report["passed"])
        self.assertIn("GOAL_GUI_UNVERIFIED", report["requirements"][0]["issues"])
        snapshot = self.snapshot(gui, gui_goal_verified=True)
        self.assertTrue(gui.assess(snapshot, verdict(snapshot))["passed"])
        pending = self.snapshot(gui, gui_goal_verified=True, pending_effects=["desktop"])
        self.assertFalse(gui.assess(pending, verdict(pending))["passed"])

    def test_no_unknown_natural_goal_can_disable_judge_without_host_requirements(self):
        with self.assertRaises(GoalContractError):
            GoalContract("anything", require_judge=False)
        with self.assertRaises(GoalContractError):
            GoalContract.from_task("Create report.txt", require_judge=False)


class GoalEvidenceTests(unittest.TestCase):
    def test_plans_finish_summaries_and_management_claims_are_not_observations(self):
        evidence = GoalEvidence()
        for name in ("update_plan", "finish_task", "ask_user_question", "request_file_access", "recall_context"):
            self.assertIsNone(evidence.observe(name, {}, "I created every required file."))
        snapshot = evidence.snapshot()
        self.assertEqual(snapshot["observations"], [])
        self.assertEqual(snapshot["sequence"], 0)

    def test_bounded_projection_marks_truncation_drops_and_hashes_full_source(self):
        evidence = GoalEvidence(max_records=2, max_characters=4096)
        evidence.observe("read_file", {"path": "a.txt"}, "A" * 10000)
        first = evidence.snapshot()
        self.assertTrue(first["observations"][0]["truncated"])
        old_hash = first["chain_sha256"]
        for index in range(4):
            evidence.observe("system_info", {}, str(index))
        snapshot = evidence.snapshot()
        self.assertEqual(len(snapshot["observations"]), 2)
        self.assertEqual(snapshot["dropped_records"], 3)
        self.assertNotEqual(snapshot["chain_sha256"], old_hash)
        snapshot["observations"].clear()
        self.assertEqual(len(evidence.snapshot()["observations"]), 2)

    def test_known_secret_redaction_and_catalog_effects_cannot_be_overridden(self):
        evidence = GoalEvidence(secrets=("TOP_SECRET",))
        evidence.observe("write_file", {"path": "report.txt", "content": "TOP_SECRET"},
                         '{"written":"report.txt","note":"TOP_SECRET"}', effect="none")
        snapshot = evidence.snapshot()
        self.assertNotIn("TOP_SECRET", json.dumps(snapshot))
        self.assertEqual(snapshot["effect_count"], 1)
        self.assertTrue(evidence.has_effects)
        evidence.observe("read_file", {}, "text")
        self.assertEqual(evidence.snapshot()["effect_count"], 1)


class GoalHarnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-goal-harness-")
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def make(self, task="Create report.txt containing exactly \"HELLO\"."):
        return TaskHarness(self.root, require_evidence=True, goal_contract=GoalContract.from_task(task))

    def test_original_sdk_false_completion_path_is_rejected(self):
        harness = self.make()
        harness.record_success("system_info", {}, '{"platform":"Windows"}')
        result = json.loads(harness.finish_task("completed", "Created report.txt with HELLO"))
        self.assertFalse(result["accepted"])
        self.assertIn("GOAL_REQUIREMENT_FAILED", result["issues"])
        self.assertIn("GOAL_JUDGE_MISSING", result["issues"])
        self.assertEqual(harness.finalize("Created report.txt with HELLO")[1], "needs_user")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_actual_file_plus_bound_judge_can_complete(self):
        harness = self.make()
        args = {"path": "report.txt", "content": "HELLO"}
        harness.record_success("write_file", args, harness.files.write(**args))
        snapshot = harness.goal_snapshot("Done")
        self.assertTrue(harness.record_goal_verification(verdict(snapshot))["passed"])
        self.assertTrue(json.loads(harness.finish_task("completed", "Done"))["accepted"])
        self.assertEqual(harness.finalize("Done")[1], "completed")
        (self.root / "report.txt").write_text("CHANGED", encoding="utf-8")
        self.assertEqual(harness.finalize("Done")[1], "needs_user")

    def test_pure_conversation_uses_answer_only_instead_of_tool_count(self):
        harness = self.make("你好")
        snapshot = harness.goal_snapshot("你好呀")
        harness.record_goal_verification(verdict(snapshot, "answer_only"))
        self.assertEqual(harness.finalize("你好呀")[1], "completed")
        self.assertFalse(harness.evidence_tools)
        self.assertNotIn("EXECUTION_EVIDENCE_MISSING", harness.assess()["issues"])

    def test_judge_cannot_override_required_tool_failure_unknown_effect_or_gui_gate(self):
        harness = self.make("检查状态")
        harness.record_success("system_info", {}, '{"platform":"Windows"}')
        harness.record_failure("write_file", {"path": "x.txt"}, classification="required")
        harness.begin_gui_action()
        snapshot = harness.goal_snapshot("Done", pending_effects=["desktop"])
        harness.record_goal_verification(verdict(snapshot))
        assessment = harness.assess()
        self.assertIn("UNRESOLVED_TOOL_FAILURE", assessment["issues"])
        self.assertIn("GUI_GOAL_UNVERIFIED", assessment["issues"])
        self.assertIn("GOAL_EFFECT_UNRESOLVED", assessment["issues"])

    def test_final_reply_change_requires_fresh_judge(self):
        harness = self.make("你好")
        snapshot = harness.goal_snapshot("你好呀")
        harness.record_goal_verification(verdict(snapshot, "answer_only"))
        assessment = harness.finalize("Another answer")[2]
        self.assertIn("GOAL_JUDGE_STALE", assessment["issues"])


if __name__ == "__main__":
    unittest.main()
