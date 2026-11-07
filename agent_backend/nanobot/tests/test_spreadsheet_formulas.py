"""Real XLSX/ZIP readback; pure host Decimal evaluation, never Excel or eval."""
from __future__ import annotations

from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import document_tools
from document_tools import DocumentTools, DocumentError
import spreadsheet_formulas as formulas


def number(value, formatting="General"):
    return {"type": "number", "value": value, "number_format": formatting}


def formula(expression, formatting="General"):
    return {"type": "formula", "expression": expression, "number_format": formatting}


def prepared(rows, name="Data"):
    return formulas.prepare_workbook([{"name": name, "rows": rows}])


class DecimalFormulaTests(unittest.TestCase):
    def test_six_functions_and_arithmetic_have_independently_expected_decimal_values(self):
        plan = prepared([[number("0.1"), number("0.2"), number("-1.235")],
                         [formula("=SUM(A1:B1)"), formula("=AVERAGE(A1:B1)"), formula("=COUNT(A1:C1)")],
                         [formula("=MIN(A1:C1)"), formula("=MAX(A1:C1)"), formula("=ROUND(C1,2)")],
                         [formula("=ROUND(1.235,2)"), formula("=ROUND(-125,-1)"), formula("=-(2+3)*4/2+11")]])
        expected = {"A2": "0.3", "B2": "0.15", "C2": "3", "A3": "-1.235", "B3": "0.2", "C3": "-1.24",
                    "A4": "1.24", "B4": "-130", "C4": "1"}
        for address, value in expected.items():
            self.assertEqual(Decimal(value), plan.sheets[0].cells[address].cache, address)

    def test_same_workbook_references_absolute_addresses_forward_dependencies_and_quoted_names(self):
        plan = formulas.prepare_workbook([
            {"name": "Summary", "rows": [[formula("='O''Brien 表'!$B$1 + Data!A2"), formula("=A1*2")]]},
            {"name": "Data", "rows": [[1], [formula("=SUM(A1,2)")]]},
            {"name": "O'Brien 表", "rows": [["Label", number("19.95")]]}])
        self.assertEqual(Decimal("22.95"), plan.sheets[0].cells["A1"].cache)
        self.assertEqual(Decimal("45.90"), plan.sheets[0].cells["B1"].cache)

    def test_text_numeric_strings_booleans_and_blank_have_explicit_semantics(self):
        plan = prepared([["10", True, None, "text", 4],
            [formula("=SUM(A1:E1)"), formula("=COUNT(A1:E1)"), formula("=AVERAGE(A1:E1)"), formula("=C1+2")],
            [formula("=SUM()"), formula("=COUNT()"), formula("=MIN()"), formula("=MAX()")]])
        self.assertEqual([Decimal(4), Decimal(1), Decimal(4), Decimal(2)], [cell.cache for cell in plan.sheets[0].rows[1]])
        self.assertEqual([Decimal(0)] * 4, [cell.cache for cell in plan.sheets[0].rows[2]])
        for value in ("10", True, "text"):
            with self.assertRaisesRegex(formulas.SpreadsheetError, "FORMULA_NON_NUMERIC_REFERENCE"):
                prepared([[value, formula("=A1+2")]])

    def test_cycles_zero_division_and_empty_average_are_rejected_before_any_writer(self):
        bad = [([[formula("=A1")]], "FORMULA_CYCLE"),
               ([[formula("=B1"), formula("=A1")]], "FORMULA_CYCLE"),
               ([[formula("=SUM(A1:B1)"), 1]], "FORMULA_CYCLE"),
               ([[formula("=1/0")]], "FORMULA_DIVISION_BY_ZERO"),
               ([[None, formula("=2/A1")]], "FORMULA_DIVISION_BY_ZERO"),
               ([["text", formula("=AVERAGE(A1:A1)")]], "FORMULA_DIVISION_BY_ZERO")]
        for rows, code in bad:
            with self.subTest(code=code), self.assertRaisesRegex(formulas.SpreadsheetError, code): prepared(rows)
        with self.assertRaisesRegex(formulas.SpreadsheetError, "FORMULA_CYCLE"):
            formulas.prepare_workbook([{"name": "A", "rows": [[formula("=B!A1")]]}, {"name": "B", "rows": [[formula("=A!A1")]]}])

    def test_external_code_dynamic_functions_and_unsupported_expressions_are_rejected(self):
        for text in ("=HYPERLINK(1)", "=INDIRECT(A1)", "=NOW()", "=WEBSERVICE(1)", "=LET(A1,1,A1)", "=SUMIF(A1,1)",
                     "='[outside.xlsx]Sheet'!A1", "=cmd|' /C calc'!A1", '=HYPERLINK("https://example.invalid", "x")',
                     "=__import__('os')", "=A1.__class__", "=2**4", "=2^4", "=A1#", "=A:A", "=MyNamedRange"):
            with self.subTest(formula=text), self.assertRaises(formulas.SpreadsheetError): prepared([[formula(text)]])

    def test_limits_cover_cells_formulas_ranges_references_ast_numbers_and_round_digits(self):
        cases = [("=SUM(A1:CV10001)", "FORMULA_RANGE_LIMIT"), ("=XFD1", "FORMULA_REFERENCE_LIMIT"),
                 ("=A1048576", "FORMULA_REFERENCE_LIMIT"), ("=1e301", "FORMULA_NUMBER_LIMIT"),
                 ("=1234567890123456", "FORMULA_NUMBER_LIMIT"), ("=ROUND(1,13)", "FORMULA_ROUND_DIGITS"),
                 ("=ROUND(1,1.5)", "FORMULA_ROUND_DIGITS"), ("=ROUND(1)", "FORMULA_ARGUMENTS"),
                 ("=SUM(A1:B2)+1", "FORMULA_CYCLE"), ("=A2:B3", "FORMULA_RANGE_REQUIRES_AGGREGATE")]
        for text, code in cases:
            with self.subTest(formula=text), self.assertRaisesRegex(formulas.SpreadsheetError, code): prepared([[formula(text)]])
        with patch.object(formulas, "MAX_CELLS", 3), self.assertRaisesRegex(formulas.SpreadsheetError, "SPREADSHEET_CELL_LIMIT"):
            prepared([[1, 2, 3, 4]])
        with patch.object(formulas, "MAX_FORMULAS", 1), self.assertRaisesRegex(formulas.SpreadsheetError, "FORMULA_COUNT_LIMIT"):
            prepared([[formula("=1"), formula("=2")]])
        with patch.object(formulas, "MAX_REFERENCES", 4), self.assertRaisesRegex(formulas.SpreadsheetError, "FORMULA_EVALUATION_LIMIT"):
            prepared([[1, 2, 3], [formula("=SUM(A1:C1)"), formula("=SUM(A1:C1)")]])
        with self.assertRaisesRegex(formulas.SpreadsheetError, "FORMULA_COMPLEXITY_LIMIT"):
            prepared([[formula("=" + "(" * 80 + "1" + ")" * 80)]])

    def test_typed_cells_are_closed_and_precision_does_not_promote_plain_strings(self):
        for value in ({"type": "number", "value": 12}, number("NaN"), number("1e301"), number("1234567890123456"),
                      {"type": "formula", "expression": "=1", "cached_value": 99},
                      {"formula": "=1"}, formula("=1", "[Red]0.00"), formula("=1", "yyyy-mm-dd")):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(formulas.SpreadsheetError): prepared([[value]])
        literal = prepared([["19.95", "=SUM(1,2)"]])
        self.assertEqual(["string", "string"], [cell.kind for cell in literal.sheets[0].rows[0]])

    def test_aggregate_input_budget_and_excel_apostrophe_name_rules(self):
        with self.assertRaisesRegex(formulas.SpreadsheetError, "SPREADSHEET_INPUT_LIMIT"):
            prepared([["x" * 32767] for _ in range(130)])
        for name in ("'leading", "trailing'"):
            with self.assertRaisesRegex(formulas.SpreadsheetError, "INVALID_SHEET_NAME"): prepared([[1]], name)
        self.assertEqual("O'Brien", prepared([[1]], "O'Brien").sheets[0].name)


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl required for actual XLSX output")
class EditableSpreadsheetDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-formula-xlsx-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.documents = DocumentTools(self.root)
        self.spec = {"sheets": [{"name": "费用", "columns": ["项目", "金额", "结果"], "rows": [
            ["商品", number("19.95", "0.00"), formula("=ROUND(SUM(B2:B3),2)", "0.00")],
            ["包装", number("0.15", "0.00"), formula("=C2*2", "#,##0.00")]]},
            {"name": "汇总", "rows": [[formula("='费用'!C2/100", "0.0%")]]}]}

    def test_editable_formulas_real_cache_styles_and_provenance_survive_independent_xml_readback(self):
        result = json.loads(self.documents.create_document("delivery.xlsx", "xlsx", self.spec))
        raw = (self.root / "delivery.xlsx").read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": formulas._S}
            cells = {item.get("r"): item for item in root.findall("s:sheetData/s:row/s:c", ns)}
            self.assertEqual("ROUND(SUM(B2:B3),2)", cells["C2"].findtext("s:f", namespaces=ns))
            self.assertEqual("20.1", cells["C2"].findtext("s:v", namespaces=ns))
            self.assertEqual("19.95", cells["B2"].findtext("s:v", namespaces=ns))
            self.assertEqual("40.2", cells["C3"].findtext("s:v", namespaces=ns))
        from openpyxl import load_workbook
        editable = load_workbook(io.BytesIO(raw), data_only=False)
        try:
            self.assertEqual("=ROUND(SUM(B2:B3),2)", editable["费用"]["C2"].value)
            self.assertEqual("f", editable["费用"]["C2"].data_type)
            self.assertEqual("0.00", editable["费用"]["C2"].number_format)
            self.assertEqual("#,##0.00", editable["费用"]["C3"].number_format)
            self.assertEqual("0.0%", editable["汇总"]["A1"].number_format)
            self.assertTrue(editable.calculation.fullCalcOnLoad)
            self.assertEqual("auto", editable.calculation.calcMode)
        finally: editable.close()
        cached = load_workbook(io.BytesIO(raw), data_only=True)
        try:
            self.assertEqual(Decimal("20.1"), Decimal(str(cached["费用"]["C2"].value)))
            self.assertEqual(Decimal("0.201"), Decimal(str(cached["汇总"]["A1"].value)))
        finally: cached.close()
        proof = result["verification"]["spreadsheet_calculation"]
        self.assertEqual("host_decimal_evaluator_v1", proof["cache_source"])
        self.assertEqual(3, proof["formula_cells"])
        self.assertFalse(proof["excel_recalculated"] or proof["visual_layout_verified"])
        readback = json.loads(self.documents.read_document("delivery.xlsx"))
        self.assertIn("cached_value=20.1", readback["text"])
        self.assertTrue(readback["formula_values_are_cached"])

    def test_formula_injection_strings_remain_literal_cells_and_never_create_links(self):
        values = ['=SUM(1,2)', '=HYPERLINK("https://example.invalid","literal")', "=cmd|' /C calc'!A1", "+1+2", "@SUM(A1)", "https://example.invalid"]
        self.documents.create_document("literal.xlsx", "xlsx", {"sheets": [{"name": "Data", "rows": [[value] for value in values]}]})
        with zipfile.ZipFile(self.root / "literal.xlsx") as archive:
            root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            self.assertEqual([], root.findall(f".//{{{formulas._S}}}f"))
            self.assertEqual([], root.findall(f".//{{{formulas._S}}}hyperlink"))
        readback = json.loads(self.documents.read_document("literal.xlsx"))
        for value in values: self.assertIn(value, readback["text"])

    def test_invalid_final_sheet_is_atomic_and_cannot_replace_existing_file(self):
        self.documents.create_document("existing.xlsx", "xlsx", {"sheets": [{"name": "Original", "rows": [["untouched"]]}]})
        original = (self.root / "existing.xlsx").read_bytes()
        digest = json.loads(self.documents.read_document("existing.xlsx"))["sha256"]
        bad = {"sheets": [{"name": "Good", "rows": [[formula("=2+3")]]}, {"name": "Bad", "rows": [[formula("=1/0")]]}]}
        with self.assertRaisesRegex(DocumentError, "FORMULA_DIVISION_BY_ZERO"):
            self.documents.create_document("existing.xlsx", "xlsx", bad, overwrite=True, expected_hash=digest)
        self.assertEqual(original, (self.root / "existing.xlsx").read_bytes())
        with self.assertRaises(DocumentError): self.documents.create_document("new.xlsx", "xlsx", bad)
        self.assertFalse((self.root / "new.xlsx").exists())

    def test_input_budget_rejects_before_workbook_serialization_and_preserves_target(self):
        self.documents.create_document("existing.xlsx", "xlsx", {"sheets": [{"name": "Original", "rows": [["untouched"]]}]})
        original = (self.root / "existing.xlsx").read_bytes()
        oversized = {"sheets": [{"name": "Large", "rows": [["x" * 32767] for _ in range(130)]}]}
        with patch.object(document_tools.DocumentTools, "_spec", side_effect=AssertionError("Budget precedes serialization")):
            with self.assertRaisesRegex(DocumentError, "SPREADSHEET_INPUT_LIMIT"):
                self.documents.create_document("existing.xlsx", "xlsx", oversized, overwrite=True)
        self.assertEqual(original, (self.root / "existing.xlsx").read_bytes())

    def test_tampered_formula_cache_and_number_format_are_detected_without_publishing(self):
        self.documents.create_document("formula.xlsx", "xlsx", self.spec)
        raw = (self.root / "formula.xlsx").read_bytes()
        plan = formulas.prepare_workbook(self.spec["sheets"])
        def tamper(change):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(raw)) as archive, zipfile.ZipFile(output, "w") as written:
                for item in archive.infolist():
                    content = archive.read(item.filename)
                    if item.filename == "xl/worksheets/sheet1.xml":
                        root = ET.fromstring(content)
                        cell = next(cell for cell in root.findall(f".//{{{formulas._S}}}c") if cell.get("r") == "C2")
                        change(cell)
                        content = ET.tostring(root)
                    written.writestr(item, content)
            return output.getvalue()
        for change in (lambda cell: setattr(cell.find(f"{{{formulas._S}}}v"), "text", "999"),
                       lambda cell: setattr(cell.find(f"{{{formulas._S}}}f"), "text", "SUM(1,2)"),
                       lambda cell: cell.set("s", "0")):
            with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(tamper(change), plan)
        with patch.object(document_tools, "cache_and_verify_xlsx", side_effect=formulas.SpreadsheetError("SPREADSHEET_READBACK_FAILED")):
            with self.assertRaisesRegex(DocumentError, "SPREADSHEET_READBACK_FAILED"):
                self.documents.create_document("not-published.xlsx", "xlsx", self.spec)
        self.assertFalse((self.root / "not-published.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
