"""Real SDK creation and separate file-content judge; no Office/model calls."""
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_goal_contract_worker as support
from test_harness_worker import tool


class XlsxFormulaWorkerTests(unittest.TestCase):
    run_case = support.GoalContractWorkerTests.run_case

    def test_actual_tool_creates_formulas_and_numeric_cache_for_independent_judge(self):
        testcase = self
        snapshots_seen = []
        def judge(model, snapshot):
            snapshots_seen.append(snapshot)
            proof = next(value for value in snapshot["facts"]["files"] if value["path"] == "pricing.xlsx")
            content = proof["content_readback"]
            testcase.assertFalse(content["rendering_performed"])
            testcase.assertFalse(content["formula_recalculation_verified"])
            cells = {item["cell"]: item for item in content["cells"]}
            testcase.assertEqual(cells["D4"]["kind"], "formula_cached")
            testcase.assertEqual(cells["D4"]["formula"], "SUM(D2:D3)")
            testcase.assertEqual(Decimal(cells["D4"]["value"]), Decimal("22.00"))
            testcase.assertEqual(cells["E2"]["kind"], "text")
            testcase.assertEqual(cells["E2"]["value"], "=SUM(1,2)")
            # This is independent XML readback of the actual saved package,
            # rather than trust in create_document's success/result metadata.
            with zipfile.ZipFile(model.workspace / "pricing.xlsx") as archive:
                root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                raw = {cell.get("r"): cell for cell in root.findall(".//s:c", ns)}
                testcase.assertEqual(raw["D4"].findtext("s:f", namespaces=ns), "SUM(D2:D3)")
                testcase.assertEqual(Decimal(raw["D2"].findtext("s:v", namespaces=ns)), Decimal("12.5"))
                testcase.assertEqual(Decimal(raw["D3"].findtext("s:v", namespaces=ns)), Decimal("9.5"))
                testcase.assertIsNone(raw["E2"].find("s:f", ns))
            return json.dumps({"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
                "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
                "status": "satisfied", "evidence_ids": snapshot["evidence_ids"]})
        number = lambda text: {"type": "number", "value": text, "number_format": "0.00"}
        formula = lambda expression: {"type": "formula", "expression": expression, "number_format": "0.00"}
        spec = {"sheets": [{"name": "Pricing", "columns": ["Item", "Price", "Quantity", "Amount", "Note"],
            "rows": [["Book", number("2.50"), 5, formula("=ROUND(B2*C2,2)"), "=SUM(1,2)"],
                     ["Pen", number("2.375"), 4, formula("=ROUND(B3*C3,2)"), "literal"],
                     ["Total", None, None, formula("=SUM(D2:D3)"), "host cache"]]}]}
        answer = "Created pricing.xlsx with editable formulas and a host-calculated total of 22.00; Excel recalculation and visual layout were not verified."
        with patch.object(support.SeparateGoalModel, "judge", judge):
            result, files, _ = self.run_case([
                tool("create_document", path="pricing.xlsx", kind="xlsx", spec=spec),
                tool("read_document", path="pricing.xlsx"),
                tool("finish_task", status="completed", summary=answer), answer],
                task="Create pricing.xlsx with Book price 2.50 quantity 5 and Pen price 2.375 quantity 4. "
                     "Use editable ROUND row amount formulas and a SUM total of 22.00. Keep Note =SUM(1,2) as literal text. "
                     "Host-calculated caches are acceptable; do not claim Excel recalculation or visual rendering.")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(files["pricing.xlsx"]["binary"])
        self.assertTrue(snapshots_seen)


if __name__ == "__main__":
    unittest.main()
