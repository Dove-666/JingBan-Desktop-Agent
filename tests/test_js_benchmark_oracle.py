"""Positive and adversarial checks of the independent saved-JavaScript oracle."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aura_js_benchmark_oracle", ROOT / "benchmarks/agent_tasks/js_oracle.py")
oracle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oracle)


@unittest.skipUnless(oracle.SDK_PYTHON.is_file(), "Pinned Agent SDK runtime unavailable")
class JsBenchmarkOracleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-js-oracle-")
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def workspace(self, case):
        task = oracle.catalog_entries()[oracle.CASES.index(case)]
        workspace = self.root / task["id"]
        workspace.mkdir()
        for fixture in task["fixtures"]:
            (workspace / fixture["path"]).write_bytes(fixture["text"].encode("utf-8"))
        return workspace

    def reference(self, case):
        workspace = self.workspace(case)
        oracle.write_reference_outputs(workspace, case)
        return workspace

    def failed(self, report, code):
        self.assertFalse(report["passed"], report)
        self.assertEqual(report["issues"], [code], report)
        self.assertEqual(report["case_count"], 0)

    def test_draft_tasks_are_new_narrow_subsets_and_leave_general_tasks_unchanged(self):
        catalog_path = ROOT / "benchmarks/agent_tasks/catalog.json"
        original = catalog_path.read_bytes()
        entries = oracle.catalog_entries()
        self.assertEqual({entry["id"] for entry in entries}, {"js_unicode_window_repair", "js_interval_union_artifact"})
        self.assertTrue(all(entry["category"] == "development_js_subset" for entry in entries))
        entries[0]["fixtures"][0]["text"] = "changed caller copy"
        self.assertNotEqual(oracle.catalog_entries()[0]["fixtures"][0]["text"], "changed caller copy")
        self.assertEqual(catalog_path.read_bytes(), original)
        tasks = json.loads(original)["tasks"]
        for identifier in ("developer_bug_fix", "developer_build_artifact"):
            self.assertEqual(next(task for task in tasks if task["id"] == identifier)["implementation"], "not_implemented")

    def test_reference_fixes_pass_real_vm_and_preserve_every_readonly_fixture(self):
        for case in oracle.CASES:
            with self.subTest(case=case):
                workspace = self.workspace(case)
                before = {path.name: path.read_bytes() for path in workspace.iterdir() if path.name != "solution.js"}
                oracle.write_reference_outputs(workspace, case)
                for name, raw in before.items():
                    self.assertEqual((workspace / name).read_bytes(), raw)
                result = oracle.evaluate_js(workspace, case, artifact=case == oracle.INTERVAL_CASE)
                self.assertTrue(result["passed"], result)
                self.assertGreaterEqual(result["case_count"], 8)
                self.assertEqual(result["source_sha256"], hashlib.sha256((workspace / "solution.js").read_bytes()).hexdigest())
                if case == oracle.INTERVAL_CASE:
                    self.assertEqual(result["artifact_sha256"], hashlib.sha256((workspace / "result.json").read_bytes()).hexdigest())
                self.assertEqual({path.name for path in workspace.iterdir()}, {"README.md", "solution.js"} |
                    ({"intervals.json", "result.json"} if case == oracle.INTERVAL_CASE else set()))

    def test_seed_functions_fail_without_any_model_or_reference_substitution(self):
        for case in oracle.CASES:
            with self.subTest(case=case):
                workspace = self.workspace(case)
                self.failed(oracle.evaluate_js(workspace, case), "JS_ORACLE_HIDDEN_CASE_MISMATCH")

    def test_strict_and_lexically_declared_saved_functions_are_supported(self):
        workspace = self.reference(oracle.UNICODE_CASE)
        source = oracle._UNICODE_REFERENCE_JS
        for value in ('"use strict";\n' + source, source.replace("function solve(input)", "const solve = function(input)")):
            (workspace / "solution.js").write_text(value, encoding="utf-8")
            result = oracle.evaluate_js(workspace, oracle.UNICODE_CASE)
            self.assertTrue(result["passed"], result)

    def test_python_references_match_hand_counted_spec_examples(self):
        self.assertEqual(oracle._unicode_reference({"text": "abba"}), {"length": 2, "start": 0, "text": "ab"})
        self.assertEqual(oracle._unicode_reference({"text": "🐳a🐳b"}), {"length": 3, "start": 1, "text": "a🐳b"})
        self.assertEqual(oracle._unicode_reference({"text": "e\u0301e\u0301"}), {"length": 2, "start": 0, "text": "e\u0301"})
        self.assertEqual(oracle._interval_reference(oracle._PUBLIC_INTERVALS), {
            "groups": [
                {"group": "__proto__", "merged": [[3, 7]], "covered": 4, "source_count": 2},
                {"group": "north", "merged": [[1, 12], [18, 20]], "covered": 13, "source_count": 4},
                {"group": "南站", "merged": [[-5, 2]], "covered": 7, "source_count": 3},
                {"group": "🐳", "merged": [], "covered": 0, "source_count": 1}],
            "total_covered": 24, "record_count": 10})

    def test_unicode_logic_tie_and_utf16_mutants_are_independently_rejected(self):
        workspace = self.reference(oracle.UNICODE_CASE)
        good = (workspace / "solution.js").read_text(encoding="utf-8")
        mutants = [good.replace("Math.max(left,last.get(points[right])+1)", "last.get(points[right])+1"),
                   good.replace("right-left+1>length", "right-left+1>=length"),
                   good.replace("Array.from(input.text)", "input.text.split('')")]
        for source in mutants:
            with self.subTest(source=source[:30]):
                self.assertNotEqual(source, good)
                (workspace / "solution.js").write_text(source, encoding="utf-8")
                report = oracle.evaluate_js(workspace, oracle.UNICODE_CASE)
                if "input.text.split('')" in source:
                    # A window cut through a surrogate pair may produce an
                    # invalid UTF-8 result before semantic comparison is possible.
                    self.assertFalse(report["passed"], report)
                    self.assertIn(report["issues"], [["JS_ORACLE_HIDDEN_CASE_MISMATCH"], ["JS_ORACLE_EVALUATION_FAILED"]])
                else:
                    self.failed(report, "JS_ORACLE_HIDDEN_CASE_MISMATCH")

    def test_interval_touch_zero_reverse_and_unicode_sort_mutants_fail(self):
        workspace = self.reference(oracle.INTERVAL_CASE)
        good = (workspace / "solution.js").read_text(encoding="utf-8")
        mutants = [good.replace("start<=prior[1]", "start<prior[1]"),
                   good.replace("if(start===end) continue;", ""),
                   good.replace("Math.min(row.start,row.end),Math.max(row.start,row.end)", "row.start,row.end"),
                   good.replace(".sort(compare)", ".sort()")]
        for source in mutants:
            with self.subTest(source=source[:30]):
                self.assertNotEqual(source, good)
                (workspace / "solution.js").write_text(source, encoding="utf-8")
                self.failed(oracle.evaluate_js(workspace, oracle.INTERVAL_CASE, artifact=True), "JS_ORACLE_HIDDEN_CASE_MISMATCH")

    def test_public_artifact_is_checked_by_python_not_a_success_claim_or_saved_function(self):
        workspace = self.reference(oracle.INTERVAL_CASE)
        correct = json.loads((workspace / "result.json").read_bytes())
        for mutate in (lambda value: value.update(total_covered=value["total_covered"] + 1),
                       lambda value: value.update(record_count=True),
                       lambda value: value["groups"].pop(),
                       lambda value: value.update(passed=True)):
            bad = deepcopy(correct)
            mutate(bad)
            (workspace / "result.json").write_text(json.dumps(bad), encoding="utf-8")
            self.failed(oracle.evaluate_js(workspace, oracle.INTERVAL_CASE, artifact=True), "JS_ORACLE_ARTIFACT_MISMATCH")
        (workspace / "result.json").write_text(json.dumps(correct), encoding="utf-8")
        (workspace / "solution.js").write_text("function solve(input) { return " + json.dumps(correct) + "; }", encoding="utf-8")
        self.failed(oracle.evaluate_js(workspace, oracle.INTERVAL_CASE, artifact=True), "JS_ORACLE_HIDDEN_CASE_MISMATCH")

    def test_hidden_cases_are_not_workspace_files_and_cannot_be_replaced_by_candidate(self):
        workspace = self.workspace(oracle.UNICODE_CASE)
        for name in ("hidden_tests.json", "js_oracle.py", "test_solution.js"):
            (workspace / name).write_text('{"passed":true,"case_count":999}', encoding="utf-8")
        (workspace / "solution.js").write_text("function solve(input) { return {passed:true,case_count:999}; }", encoding="utf-8")
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_HIDDEN_CASE_MISMATCH")
        (workspace / "solution.js").write_text("return [{passed:true}];", encoding="utf-8")
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_EVALUATION_FAILED")
        (workspace / "solution.js").write_text("JSON.stringify=()=>'{\"passed\":true}'; function solve(input){return {passed:true};}", encoding="utf-8")
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_HIDDEN_CASE_MISMATCH")

    def test_readme_and_source_fixture_changes_fail_before_any_candidate_execution(self):
        for case, name in ((oracle.UNICODE_CASE, "README.md"), (oracle.INTERVAL_CASE, "intervals.json")):
            with self.subTest(name=name):
                workspace = self.reference(case)
                (workspace / name).write_text("edited fixture", encoding="utf-8")
                with patch.object(oracle, "_execute") as execute:
                    self.failed(oracle.evaluate_js(workspace, case), "JS_ORACLE_SOURCE_CHANGED")
                    execute.assert_not_called()

    def test_syntax_host_io_infinite_loop_and_secrets_are_static_failures(self):
        workspace = self.reference(oracle.UNICODE_CASE)
        secret = "private-source-error-canary"
        source = workspace / "solution.js"
        for code, expected in (("function solve( {", "JS_ORACLE_EVALUATION_FAILED"),
                               ("function solve(input){throw new Error('" + secret + "')}", "JS_ORACLE_EVALUATION_FAILED"),
                               ("function solve(input){return require('fs')}", "JS_ORACLE_EVALUATION_FAILED"),
                               ("function solve(input){while(true){}}", "JS_ORACLE_TIME_LIMIT")):
            source.write_text(code, encoding="utf-8")
            report = oracle.evaluate_js(workspace, oracle.UNICODE_CASE)
            self.failed(report, expected)
            self.assertNotIn(secret, json.dumps(report))
            self.assertNotIn(str(workspace), json.dumps(report))
            self.assertNotIn(code, json.dumps(report))

    def test_real_child_canary_file_and_network_remain_untouched(self):
        workspace = self.reference(oracle.UNICODE_CASE)
        canary = self.root / "outside-private-file.txt"
        canary.write_text("canary content", encoding="utf-8")
        source = workspace / "solution.js"
        source.write_text("function solve(input){ require('fs').writeFileSync(" + json.dumps(str(canary)) + ",'changed');return {}; }", encoding="utf-8")
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_EVALUATION_FAILED")
        self.assertEqual(canary.read_text(), "canary content")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            listener.settimeout(0.1)
            source.write_text("function solve(input){return fetch('http://127.0.0.1:" + str(listener.getsockname()[1]) + "/canary')}", encoding="utf-8")
            self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_EVALUATION_FAILED")
            with self.assertRaises(socket.timeout):
                listener.accept()

    def test_changed_saved_source_cannot_pass_with_a_stale_successful_run(self):
        workspace = self.reference(oracle.UNICODE_CASE)
        execute = oracle._execute
        def change_after_execution(source, cases):
            value = execute(source, cases)
            (workspace / "solution.js").write_text("function solve(input){return {}}", encoding="utf-8")
            return value
        with patch.object(oracle, "_execute", side_effect=change_after_execution):
            self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_SOLUTION_CHANGED_DURING_CHECK")

    def test_changed_artifact_cannot_pass_an_earlier_snapshot(self):
        workspace = self.reference(oracle.INTERVAL_CASE)
        read, changed = oracle._read, False
        def mutate_after_read(folder, name, maximum):
            nonlocal changed
            value = read(folder, name, maximum)
            if name == "result.json" and not changed:
                changed = True
                (workspace / name).write_text("{}", encoding="utf-8")
            return value
        with patch.object(oracle, "_read", side_effect=mutate_after_read):
            self.failed(oracle.evaluate_js(workspace, oracle.INTERVAL_CASE, artifact=True), "JS_ORACLE_ARTIFACT_CHANGED_DURING_CHECK")

    def test_file_limits_encoding_and_workspace_escape_are_rejected(self):
        workspace = self.reference(oracle.UNICODE_CASE)
        source = workspace / "solution.js"
        source.write_bytes(b"x" * (oracle.MAX_SOURCE_BYTES + 1))
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_FILE_LIMIT")
        source.write_bytes(b"\xff\xfe")
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_INVALID_SOURCE_ENCODING")
        with self.assertRaises(oracle.JsOracleError):
            oracle._read(workspace, "../private.js", oracle.MAX_SOURCE_BYTES)
        outside = self.root / "outside.js"
        outside.write_text("function solve(input){return {}}", encoding="utf-8")
        source.unlink()
        try:
            source.symlink_to(outside)
        except OSError:
            return  # Other escape checks still ran on hosts without symlink rights.
        self.failed(oracle.evaluate_js(workspace, oracle.UNICODE_CASE), "JS_ORACLE_WORKSPACE_ESCAPE")


if __name__ == "__main__":
    unittest.main()
