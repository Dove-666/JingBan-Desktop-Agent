"""High-precision file guards must not mistake data or code for artifacts."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_backend" / "nanobot"))
from goal_contract import GoalContract, infer_file_requirements
from tools.run_agent_benchmarks import load_catalog


class GoalContractTokenTests(unittest.TestCase):
    def test_original_xlsx_budget_prompt_has_only_the_actual_workbook_requirement(self):
        catalog, _ = load_catalog()
        task = next(item for item in catalog["tasks"] if item["id"] == "xlsx_budget")
        requirements = GoalContract.from_task(task["prompt"]).requirements
        self.assertEqual([(item["kind"], item["path"]) for item in requirements], [("artifact", "budget.xlsx")])

    def test_chinese_amounts_versions_domains_and_code_members_are_not_files(self):
        task = "创建预算.xlsx，金额为12.50和7.25，版本v1.2.3，网址example.com，代码console.log和obj.value。"
        self.assertEqual([item["path"] for item in infer_file_requirements(task)], ["预算.xlsx"])
        for task in ("Create 12.50", "Create v1.2.3", "Read example.com", "Read console.log",
                     "Read obj.value", "Read response.json()", "Create version.abc123",
                     "Create report.txt with version `1.2` and amount 12.50"):
            with self.subTest(task=task):
                paths = [item["path"] for item in infer_file_requirements(task)]
                self.assertEqual(paths, ["report.txt"] if "report.txt" in task else [])

    def test_real_numeric_filenames_need_quotes_or_explicit_file_context(self):
        for task, expected in (("Create `12.50`", "12.50"), ('Create "2026.11"', "2026.11"),
                               ("Create a file named 12.50", "12.50"), ("创建文件 12.50", "12.50"),
                               ("创建 12.50 的文件", "12.50"), ("Read the file v1.2.3", "v1.2.3")):
            with self.subTest(task=task):
                self.assertEqual([item["path"] for item in infer_file_requirements(task)], [expected])

    def test_unknown_extension_or_domain_needs_explicit_file_context(self):
        self.assertEqual(infer_file_requirements("Create unknown.anysuffix"), [])
        self.assertEqual([item["path"] for item in infer_file_requirements("Create a file named unknown.anysuffix")], ["unknown.anysuffix"])
        self.assertEqual([item["path"] for item in infer_file_requirements("Create the file example.com")], ["example.com"])
        self.assertEqual(infer_file_requirements("Read `example.com`"), [])


if __name__ == "__main__":
    unittest.main()
