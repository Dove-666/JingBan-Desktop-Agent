"""Benchmark schema and independent oracles; no real API, Qt or desktop use."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aura_agent_benchmark_runner", ROOT / "tools" / "run_agent_benchmarks.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class AgentBenchmarkRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-benchmark-oracle-")
        self.root = Path(self.directory.name)
        self.catalog, _ = runner.load_catalog()

    def tearDown(self):
        self.directory.cleanup()

    def test_execution_diagnostics_are_fixed_bounded_and_do_not_guess_root_cause(self):
        good = {"phase": "runtime", "code": "SDK_TURN_FAILED_UNKNOWN_CAUSE"}
        values = [good] * 70 + [
            {**good, "body": "SECRET"}, {"phase": "SECRET", "code": good["code"]},
            {"phase": "model", "code": "SECRET"}, {"phase": [], "code": []}]
        result = runner.project_execution_diagnostics(values)
        self.assertEqual(result["count"], 70)
        self.assertEqual(len(result["events"]), 64)
        self.assertTrue(result["truncated"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertIn("not_inferred_root_cause", result["scope"])
        cleanup = runner.project_execution_diagnostics([
            {"phase": "runtime", "code": "WORKER_CLEANUP_UNCONFIRMED"}])
        self.assertEqual(cleanup["events"], [{"phase": "runtime", "code": "WORKER_CLEANUP_UNCONFIRMED"}])

    def test_judge_diagnostics_preserve_explicit_unknown_vs_failed_receipt(self):
        from agent_backend.nanobot.goal_judge import JUDGE_DIAGNOSTIC_CODES
        rows = [{"phase": "model", "code": code} for code in sorted(JUDGE_DIAGNOSTIC_CODES)]
        rows.extend([
            {"phase": "model", "code": "GOAL_JUDGE_INVALID_JSON", "raw": "SECRET"},
            {"phase": "model", "code": "GOAL_JUDGE_" + "SECRET"},
        ])
        result = runner.project_execution_diagnostics(rows)
        self.assertEqual(result["events"], rows[:-2])
        self.assertEqual(result["count"], len(JUDGE_DIAGNOSTIC_CODES))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertIn({"phase": "model", "code": "GOAL_JUDGE_EXPLICIT_UNKNOWN"}, result["events"])
        self.assertIn({"phase": "model", "code": "GOAL_JUDGE_INVALID_JSON"}, result["events"])

    def test_case_source_change_invalidates_pass_without_erasing_oracle(self):
        executor = runner.ConfiguredExecutor.__new__(runner.ConfiguredExecutor)
        evidence = {"status": "passed", "independent_oracle": {"passed": True}}
        initial = {"sha256": "a" * 64, "files": {"worker.py": "1" * 64}}
        final = {"sha256": "b" * 64, "files": {"worker.py": "2" * 64}}
        with patch.object(executor, "_run", return_value=deepcopy(evidence)) as execute, \
                patch.object(runner, "runtime_fingerprint", side_effect=[initial, final]):
            result = executor.run({}, self.root, {})
        execute.assert_called_once()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "BENCHMARK_SOURCE_CHANGED")
        self.assertEqual(result["execution_status_before_source_check"], "passed")
        self.assertTrue(result["independent_oracle"]["passed"])
        self.assertFalse(result["source_snapshot"]["unchanged"])

    def test_fingerprint_is_stable_and_contains_no_settings_or_workspace_paths(self):
        first = runner.runtime_fingerprint()
        self.assertEqual(first, runner.runtime_fingerprint())
        self.assertFalse(first["loaded_binary_attestation"])
        self.assertIn("agent_backend/nanobot/worker.py", first["files"])
        self.assertIn("agent_backend/nanobot/browser_dom.js", first["files"])
        for path, digest in first["files"].items():
            self.assertFalse(Path(path).is_absolute())
            self.assertNotIn("settings.ini", path)
            self.assertNotIn("agent_workspace", path)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def task(self, identifier):
        return deepcopy(next(task for task in self.catalog["tasks"] if task["id"] == identifier))

    def reference(self, identifier):
        task = self.task(identifier)
        workspace = self.root / identifier
        before = runner.prepare_fixtures(task, workspace)
        answer = runner._reference_outputs(task, workspace)
        return task, workspace, before, answer

    def test_catalog_marks_missing_capabilities_without_substitute_baselines(self):
        tasks = self.catalog["tasks"]
        self.assertGreaterEqual(len(tasks), 20)
        self.assertEqual(len({task["id"] for task in tasks}), len(tasks))
        self.assertTrue({"browser", "development", "gui", "sync"} <= {task["category"] for task in tasks})
        missing = [task for task in tasks if task["implementation"] == "not_implemented"]
        self.assertGreaterEqual(len(missing), 8)
        self.assertTrue(all(task["blocked_reason"] for task in missing))

    def test_catalog_v2_appends_js_subset_without_rewriting_previous_27_tasks(self):
        self.assertEqual(self.catalog["version"], 3)
        previous = self.catalog["tasks"][:27]
        self.assertEqual(len(previous), 27)
        digest = hashlib.sha256(json.dumps(previous, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(digest, "32eda9008ef3804bad8e62fbb34be4abbaed6813991cac282347721e4d7b6ebc")
        self.assertEqual(self.catalog["tasks"][27:29], runner._js_oracle().catalog_entries())
        for identifier in ("developer_bug_fix", "developer_build_artifact"):
            self.assertEqual(self.task(identifier)["implementation"], "not_implemented")

    def test_v3_formula_subset_preserves_all_29_v2_tasks_and_checks_independent_mutants(self):
        previous = self.catalog["tasks"][:29]
        digest = hashlib.sha256(json.dumps(previous, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(digest, "18fe8fe554b2b32f5305e85fa71a158eb7cef2f5d0b38fe915726662b07385a6")
        self.assertEqual(self.catalog["tasks"][29:], [runner._xlsx_oracle().catalog_entry()])
        task = self.task("xlsx_editable_formula_budget")
        proof = runner.oracle_self_test(task, self.root)
        self.assertEqual(proof["status"], "passed", proof)
        self.assertTrue(proof["xlsx_reference_and_mutants"]["passed"])
        self.assertEqual(proof["xlsx_reference_and_mutants"]["model_calls"], 0)
        self.assertEqual(len(proof["xlsx_reference_and_mutants"]["checks"]), 6)
        for mutate in (lambda row: row["oracles"][0].update(path="../outside.xlsx"),
                       lambda row: row.update(category="general_excel_editing"),
                       lambda row: row["fixtures"][0].update(text="modified source")):
            broken = deepcopy(self.catalog)
            row = next(item for item in broken["tasks"] if item["id"] == task["id"])
            mutate(row)
            with self.assertRaises(runner.BenchmarkError):
                runner.validate_catalog(broken)

    def test_js_kinds_reject_unknown_fields_cases_paths_and_broader_task_labels(self):
        for identifier, mutate in (
            ("js_unicode_window_repair", lambda task: task["oracles"][0].update(path="../solution.js")),
            ("js_unicode_window_repair", lambda task: task["oracles"][0].update(case="load_python")),
            ("js_unicode_window_repair", lambda task: task["oracles"][0].update(executable="node")),
            ("js_unicode_window_repair", lambda task: task.update(category="development")),
            ("js_unicode_window_repair", lambda task: task["fixtures"][0].update(text="different contract")),
            ("js_interval_union_artifact", lambda task: task["oracles"][1].update(source_path="../private.json")),
            ("js_interval_union_artifact", lambda task: task["oracles"][1].update(case="unicode_distinct_window_v1")),
            ("js_interval_union_artifact", lambda task: task["oracles"].pop(0)),
        ):
            with self.subTest(identifier=identifier):
                broken = deepcopy(self.catalog)
                task = next(row for row in broken["tasks"] if row["id"] == identifier)
                mutate(task)
                with self.assertRaises(runner.BenchmarkError):
                    runner.validate_catalog(broken)

    def test_js_self_tests_reject_bad_source_and_valid_public_only_mutants(self):
        for identifier in ("js_unicode_window_repair", "js_interval_union_artifact"):
            with self.subTest(identifier=identifier):
                report = runner.oracle_self_test(self.task(identifier), self.root)
                self.assertEqual(report["status"], "passed", report)
                self.assertTrue(report["positive_accepted"])
                self.assertTrue(report["negative_rejected"])
                self.assertTrue(report["hidden_mutant_rejected"])
                self.assertFalse(report["model_called"])

    def test_js_correct_public_artifact_cannot_replace_hidden_function_success(self):
        task, workspace, before, answer = self.reference("js_interval_union_artifact")
        self.assertTrue(runner.evaluate_oracles(task, workspace, answer, before)["passed"])
        artifact = (workspace / "result.json").read_bytes()
        runner._js_oracle().write_public_only_mutant(workspace, "grouped_interval_union_v1")
        self.assertEqual((workspace / "result.json").read_bytes(), artifact)
        result = runner.evaluate_oracles(task, workspace, "All tests passed", before)
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"][0]["issues"], ["JS_ORACLE_HIDDEN_CASE_MISMATCH"])
        self.assertTrue(result["checks"][-1]["passed"])

    def test_all_implemented_real_oracles_accept_reference_and_reject_mutant(self):
        for task in self.catalog["tasks"]:
            if task["suite"] == "real_model" and task["implementation"] == "implemented":
                with self.subTest(task=task["id"]):
                    result = runner.oracle_self_test(task, self.root)
                    self.assertEqual(result["status"], "passed", result)
                    self.assertFalse(result["model_called"])

    def test_guard_suite_is_negative_and_separate_from_model_capability(self):
        for task in self.catalog["tasks"]:
            if task["suite"] == "guard":
                with self.subTest(task=task["id"]):
                    report = runner.run_guard(task, self.root)
                    self.assertTrue(report["passed"], report)
                    self.assertFalse(report["model_called"])
        summary = runner.summarize([
            {"task_id": "guard", "suite": "guard", "status": "passed", "real_model_attempted": False},
            {"task_id": "real", "suite": "real_model", "status": "not_run", "real_model_attempted": False}])
        self.assertEqual(summary["real_model_passed"], 0)
        self.assertIsNone(summary["real_model_success_rate"])

    def test_catalog_refuses_escape_unknown_oracles_and_duplicate_tasks(self):
        for mutate in (
            lambda catalog: catalog["tasks"].append(deepcopy(catalog["tasks"][0])),
            lambda catalog: catalog["tasks"][0]["oracles"][0].update(kind="accept_model_summary"),
            lambda catalog: catalog["tasks"][1]["fixtures"][0].update(path="../personal.txt"),
            lambda catalog: catalog["tasks"][1]["fixtures"][0].update(generator="execute_python"),
        ):
            broken = deepcopy(self.catalog)
            mutate(broken)
            with self.assertRaises(runner.BenchmarkError):
                runner.validate_catalog(broken)

    def test_answer_json_keeps_bool_distinct_from_numeric_and_requires_json(self):
        task, workspace, before, _ = self.reference("answer_arithmetic")
        self.assertTrue(runner.evaluate_oracles(task, workspace, '{"answer":84}', before)["passed"])
        for answer in ('{"answer":true}', '{"answer":83}', 'The answer is 84.', '{"answer":84,"answer":84}'):
            with self.subTest(answer=answer):
                self.assertFalse(runner.evaluate_oracles(task, workspace, answer, before)["passed"])

    def test_file_text_is_exact_bytes_not_nonempty_file_or_model_claim(self):
        task, workspace, before, answer = self.reference("file_create_exact")
        path = workspace / "report.txt"
        path.write_bytes(b"HELLO\n")
        self.assertFalse(runner.evaluate_oracles(task, workspace, "Created exactly HELLO", before)["passed"])
        path.unlink()
        self.assertFalse(runner.evaluate_oracles(task, workspace, answer, before)["passed"])

    def test_decimal_totals_are_independent_of_model_and_float_rounding(self):
        task, workspace, before, answer = self.reference("decimal_csv_totals")
        self.assertTrue(runner.evaluate_oracles(task, workspace, answer, before)["passed"])
        actual = json.loads((workspace / "totals.json").read_text())
        for field, value in (("total", "30.399999999999999"), ("record_count", True), ("total", 30.4)):
            with self.subTest(field=field, value=value):
                bad = {**actual, field: value}
                (workspace / "totals.json").write_text(json.dumps(bad), encoding="utf-8")
                self.assertFalse(runner.evaluate_oracles(task, workspace, answer, before)["passed"])

    def test_unchanged_source_hash_is_required_even_when_answer_is_right(self):
        task, workspace, before, answer = self.reference("read_sources_compare")
        (workspace / "north.txt").write_text("changed source", encoding="utf-8")
        report = runner.evaluate_oracles(task, workspace, answer, before)
        self.assertFalse(report["passed"])
        self.assertEqual(report["checks"][-1]["issues"], ["SOURCE_CHANGED"])

    def test_multiple_artifacts_cannot_pass_when_one_is_missing(self):
        task, workspace, before, answer = self.reference("multi_artifact_delivery")
        (workspace / "second.txt").unlink()
        report = runner.evaluate_oracles(task, workspace, answer, before)
        self.assertFalse(report["passed"])
        self.assertTrue(report["checks"][0]["passed"])
        self.assertFalse(report["checks"][1]["passed"])

    def test_xlsx_numeric_string_or_formula_is_not_a_numeric_literal(self):
        task, workspace, before, answer = self.reference("xlsx_budget")
        target = workspace / "budget.xlsx"
        with zipfile.ZipFile(target) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        sheet = original["xl/worksheets/sheet1.xml"].decode()
        for wrong in ('<c r="B2" t="inlineStr"><is><t>12.50</t></is></c>',
                      '<c r="B2"><f>10+2.5</f><v>12.50</v></c>'):
            with self.subTest(wrong=wrong):
                parts = {**original, "xl/worksheets/sheet1.xml": sheet.replace('<c r="B2"><v>12.50</v></c>', wrong)}
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, raw in parts.items():
                        archive.writestr(name, raw)
                self.assertFalse(runner.evaluate_oracles(task, workspace, answer, before)["passed"])

    def test_long_context_fixture_is_deterministic_unique_and_longer_than_one_page(self):
        task = self.task("long_context_needle")
        workspace = self.root / "long"
        before = runner.prepare_fixtures(task, workspace)
        raw = (workspace / "long_notes.txt").read_bytes()
        self.assertGreater(len(raw), 24000)
        self.assertEqual(raw.count(b"FINAL_RECOVERY_CODE="), 1)
        self.assertEqual(before["long_notes.txt"], runner._hash(raw))

    def test_repetition_and_not_implemented_are_not_inflated_into_catalog_passes(self):
        cases = [{"task_id": "a", "suite": "real_model", "status": "passed", "real_model_attempted": True},
                 {"task_id": "a", "suite": "real_model", "status": "failed", "real_model_attempted": True},
                 {"task_id": "b", "suite": "real_model", "status": "not_implemented", "real_model_attempted": False}]
        summary = runner.summarize(cases)
        self.assertEqual(summary["catalog_real_tasks"], 2)
        self.assertEqual(summary["real_model_cases"], 3)
        self.assertEqual(summary["real_model_success_rate"], .5)
        self.assertEqual(summary["catalog_coverage_pass_rate"], 0)

    def test_default_cli_never_loads_credentials_and_reports_zero_real_attempts(self):
        output = self.root / "report.json"
        with patch.object(runner, "ConfiguredExecutor", side_effect=AssertionError("Credentials must remain untouched")), \
                patch("sys.stdout", new=io.StringIO()):
            code = runner.main(["--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["provider"], "offline")
        self.assertEqual(report["summary"]["real_model_attempted"], 0)
        self.assertIsNone(report["summary"]["real_model_success_rate"])
        self.assertFalse(report["logs_include_reasoning"])
        self.assertEqual(report["catalog_version"], 3)
        self.assertEqual(len(report["javascript_subset_verifier"]["implementation_sha256"]), 64)
        self.assertFalse(report["javascript_subset_verifier"]["executes_candidate_python_or_shell"])
        self.assertTrue(all(value["status"] == "not_run" and not value["substitute_used"] for value in report["baselines"].values()))
        self.assertTrue(all(row["status"] != "passed" for row in report["cases"] if row["suite"] == "real_model"))

    def test_subset_success_keeps_the_full_catalog_denominator_and_unverified_scope(self):
        cases = [{"task_id": "a", "suite": "real_model", "status": "passed", "real_model_attempted": True}] * 3
        summary = runner.summarize(cases, {"a": 3}, full_catalog_real_tasks=26)
        self.assertEqual(summary["real_model_success_rate"], 1)
        self.assertEqual(summary["catalog_coverage_pass_rate"], 1)
        self.assertEqual(summary["catalog_coverage_scope"], "selected_task_definitions_only")
        self.assertEqual(summary["full_catalog_unselected_tasks"], 25)
        self.assertEqual(summary["fully_verified_task_types"], 1)
        self.assertEqual(summary["full_catalog_verified_task_coverage"], 1 / 26)
        unfinished = runner.summarize(cases[:2], {"a": 3}, full_catalog_real_tasks=26)
        self.assertEqual(unfinished["fully_verified_task_types"], 0)
        self.assertEqual(unfinished["full_catalog_verified_task_coverage"], 0)
        offline = runner.summarize([{**cases[0], "real_model_attempted": False}], {"a": 1}, full_catalog_real_tasks=26)
        self.assertIsNone(offline["full_catalog_verified_task_coverage"])

    def test_selected_offline_cli_still_records_all_catalog_types_without_credentials(self):
        output = self.root / "subset.json"
        with patch.object(runner, "ConfiguredExecutor", side_effect=AssertionError("No credentials for offline selection")), \
                patch("sys.stdout", new=io.StringIO()):
            code = runner.main(["--task", "xlsx_editable_formula_budget", "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        scope = report["catalog_scope"]
        self.assertEqual(scope["real_task_types"], 26)
        self.assertEqual(scope["selected_real_task_types"], 1)
        self.assertEqual(scope["implemented_task_types"], 14)
        self.assertEqual(scope["not_implemented_task_types"], 12)
        self.assertTrue(scope["is_subset"])
        self.assertTrue(scope["unselected_tasks_are_unverified"])
        self.assertIsNone(report["summary"]["full_catalog_verified_task_coverage"])

    def test_model_identifier_never_echoes_a_key_or_arbitrary_provider_text(self):
        self.assertEqual(runner._model_identifier("model-TOP_SECRET", ("TOP_SECRET",)), "configured-model-redacted")
        self.assertNotIn("provider\nPRIVATE", runner._model_identifier("provider\nPRIVATE", ()))

    def test_partial_report_keeps_unfinished_cases_out_of_coverage_and_saves_atomically(self):
        output = self.root / "report.json"
        report = {"cases": [{"task_id": "a", "suite": "real_model", "status": "passed", "real_model_attempted": True}],
                  "planned_real_cases": {"a": 2, "b": 1}, "run_status": "running", "validation_passed": None}
        runner.save_report(report, output)
        saved = json.loads(output.read_text())
        self.assertEqual(saved["run_status"], "running")
        self.assertIsNone(saved["validation_passed"])
        self.assertEqual(saved["summary"]["cases_not_finished"], 2)
        self.assertEqual(saved["summary"]["catalog_coverage_pass_rate"], 0)
        self.assertEqual(list(self.root.glob(".benchmark-report-*.tmp")), [])
        old_bytes = output.read_bytes()
        with patch.object(runner.os, "replace", side_effect=OSError("owned simulated interruption")):
            with self.assertRaises(OSError):
                runner.save_report(report, output)
        self.assertEqual(output.read_bytes(), old_bytes)
        self.assertEqual(list(self.root.glob(".benchmark-report-*.tmp")), [])

    def test_finish_diagnostics_keep_only_fixed_codes_statuses_and_counts(self):
        secret = "TOP_SECRET_PROVIDER_TEXT"
        value = {"accepted": False, "status": "needs_user", "passed": False,
            "summary": secret, "paths": ["C:/Users/private.txt"], "issues": ["PLAN_UNFINISHED", secret],
            "goal": {"judge_status": "answer_only", "issues": ["GOAL_ANSWER_ONLY_INVALID", secret],
                "requirements": [{"path": "C:/Users/private.txt", "id": secret, "passed": True, "issues": []},
                                 {"path": secret, "passed": False, "issues": ["GOAL_FILE_TEXT_MISMATCH", secret]}],
                "rawreceipt": secret}}
        records = [{"role": "assistant", "content": secret},
                   {"role": "tool", "name": "read_file", "content": secret},
                   {"role": "tool", "name": "finish_task", "content": json.dumps(value)}]
        result = runner.project_finish_diagnostics(records)
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("private.txt", encoded)
        self.assertNotIn("rawreceipt", encoded)
        self.assertNotIn("summary", encoded)
        self.assertEqual(result["observed_count"], 1)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["codes"], ["GOAL_ANSWER_ONLY_INVALID", "PLAN_UNFINISHED"])
        self.assertEqual(attempt["judge_status"], "answer_only")
        self.assertEqual((attempt["requirement_total"], attempt["requirement_passed"], attempt["requirement_failed"]), (2, 1, 1))
        self.assertEqual(attempt["requirement_codes"], ["GOAL_FILE_TEXT_MISMATCH"])

    def test_malformed_or_untrusted_finish_fields_never_enter_diagnostic_log(self):
        secret = "TOP_SECRET_PROVIDER_TEXT"
        records = [{"role": "tool", "name": "finish_task", "content": secret},
                   {"role": "tool", "name": "finish_task", "content": json.dumps({
                       "accepted": secret, "status": secret, "passed": secret, "issues": [secret],
                       "goal": {"judge_status": secret, "requirements": [{"passed": secret}]}})}]
        report = runner.project_finish_diagnostics(records)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report["attempts"][0]["codes"], ["FINISH_DIAGNOSTIC_UNAVAILABLE"])
        self.assertEqual(report["attempts"][1]["judge_status"], "unavailable")

    def test_finish_diagnostics_are_bounded_and_explicitly_not_the_final_assessment(self):
        records = [{"role": "tool", "name": "finish_task", "content": '{"accepted":false}'} for _ in range(40)]
        report = runner.project_finish_diagnostics(records)
        self.assertEqual(report["observed_count"], 40)
        self.assertEqual(report["retained_count"], 32)
        self.assertTrue(report["truncated"])
        self.assertIn("final_check_may_differ", report["scope"])


if __name__ == "__main__":
    unittest.main()
