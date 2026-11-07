"""Current Office content proof without model prose, rendering or desktop use."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_backend" / "nanobot"))
from goal_contract import GoalContract
from goal_evidence import GoalEvidence
from safe_files import WorkspaceFiles
from tools.run_agent_benchmarks import load_catalog, prepare_fixtures, _reference_outputs


class GoalDocumentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-goal-office-proof-")
        self.root = Path(self.directory.name)
        self.catalog, _ = load_catalog()

    def tearDown(self):
        self.directory.cleanup()

    def materialize(self, identifier):
        task = deepcopy(next(item for item in self.catalog["tasks"] if item["id"] == identifier))
        workspace = self.root / identifier
        prepare_fixtures(task, workspace)
        _reference_outputs(task, workspace)
        contract = GoalContract.from_task(task["prompt"])
        return task, workspace, contract

    def snapshot(self, workspace, contract):
        return contract.snapshot(WorkspaceFiles(workspace), GoalEvidence(), answer="Saved and inspected the requested content.")

    def test_current_docx_pptx_and_xlsx_content_is_visible_without_rendering(self):
        for identifier, required in (("docx_report", "The water sample is clear."),
                                     ("pptx_three_slides", "Repeat tomorrow"), ("xlsx_budget", "B4=19.75")):
            with self.subTest(task=identifier):
                _, workspace, contract = self.materialize(identifier)
                with patch("subprocess.Popen", side_effect=AssertionError("No Office process or renderer may start")):
                    snapshot = self.snapshot(workspace, contract)
                content = snapshot["facts"]["files"][0]["content_readback"]
                self.assertEqual(content["status"], "available")
                self.assertIn(required, content["text"])
                self.assertFalse(content["visual_layout_verified"])
                self.assertFalse(content["rendering_performed"])
                self.assertFalse(content["truncated"])

    def test_xlsx_numeric_literal_text_and_formula_cache_are_distinguished(self):
        _, workspace, contract = self.materialize("xlsx_budget")
        target = workspace / "budget.xlsx"
        with zipfile.ZipFile(target) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        literal = self.snapshot(workspace, contract)["facts"]["files"][0]["content_readback"]
        cell = next(item for item in literal["cells"] if item["cell"] == "B2")
        self.assertEqual(cell["kind"], "number_literal")
        sheet = original["xl/worksheets/sheet1.xml"].decode()
        for replacement, expected in ((
            '<c r="B2" t="inlineStr"><is><t>12.50</t></is></c>', "text"),
            ('<c r="B2"><f>10+2.5</f><v>12.50</v></c>', "formula_cached")):
            with self.subTest(expected=expected):
                parts = {**original, "xl/worksheets/sheet1.xml": sheet.replace('<c r="B2"><v>12.50</v></c>', replacement)}
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, raw in parts.items():
                        archive.writestr(name, raw)
                content = self.snapshot(workspace, contract)["facts"]["files"][0]["content_readback"]
                self.assertEqual(next(item for item in content["cells"] if item["cell"] == "B2")["kind"], expected)
                self.assertFalse(content["formula_recalculation_verified"])

    def test_office_content_and_versions_come_from_actual_file_not_writer_args(self):
        _, workspace, contract = self.materialize("docx_report")
        first = self.snapshot(workspace, contract)
        target = workspace / "report.docx"
        with zipfile.ZipFile(target) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        parts["word/document.xml"] = parts["word/document.xml"].replace(b"The water sample is clear.", b"ACTUAL LATER CONTENT")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, raw in parts.items():
                archive.writestr(name, raw)
        later = self.snapshot(workspace, contract)
        self.assertNotEqual(first["evidence_id"], later["evidence_id"])
        self.assertIn("ACTUAL LATER CONTENT", later["facts"]["files"][0]["content_readback"]["text"])

    def test_read_file_requirements_allow_a_grounded_read_only_json_answer(self):
        workspace = self.root / "read-only"
        workspace.mkdir()
        (workspace / "north.txt").write_text("temperature=18.75", encoding="utf-8")
        (workspace / "south.txt").write_text("temperature=21.10", encoding="utf-8")
        contract = GoalContract.from_task("Read north.txt and south.txt and report the difference as JSON.")
        self.assertTrue(contract.requirements)
        self.assertTrue(all(item["kind"] == "read_file" for item in contract.requirements))
        evidence = GoalEvidence()
        files = WorkspaceFiles(workspace)
        for name in ("north.txt", "south.txt"):
            evidence.observe("read_file", {"path": name}, files.read_page(name))
        snapshot = contract.snapshot(files, evidence, answer='{"difference":"2.35"}')
        receipt = {"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"], "evidence_id": snapshot["evidence_id"],
            "answer_sha256": snapshot["answer_sha256"], "status": "answer_only", "evidence_ids": snapshot["evidence_ids"]}
        self.assertTrue(contract.assess(snapshot, receipt)["passed"])


if __name__ == "__main__":
    unittest.main()
