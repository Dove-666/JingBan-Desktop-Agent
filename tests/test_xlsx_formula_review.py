"""Independent fixed semantic/negative XLSX review. No Office or models.

Expected amounts are hand-calculated. ZIP/XML assertions do not ask the
producer's evaluator or formatter for the expected cached values or styles.
"""
from decimal import Decimal
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"))
import document_tools as documents
import spreadsheet_formulas as formulas

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def num(value, fmt="General"):
    return {"type": "number", "value": value, "number_format": fmt}


def fn(value, fmt="General"):
    return {"type": "formula", "expression": value, "number_format": fmt}


def workbook(rows, name="Data"):
    return [{"name": name, "rows": rows}]


def mutate(raw, changes):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as destination:
        for info in source.infolist():
            value = source.read(info.filename)
            if info.filename in changes:
                root = ET.fromstring(value)
                changes[info.filename](root)
                value = ET.tostring(root)
            destination.writestr(info, value)
    return output.getvalue()


class FormulaBoundaryReviewTests(unittest.TestCase):
    def test_pure_ast_does_not_call_python_execution_or_host_io(self):
        with patch("builtins.eval", side_effect=AssertionError("No eval")), patch("builtins.exec", side_effect=AssertionError("No exec")), patch("builtins.open", side_effect=AssertionError("No IO")), patch("subprocess.Popen", side_effect=AssertionError("No process")):
            plan = formulas.prepare_workbook(workbook([[num("19.95"), num("0.05"), fn("=ROUND(SUM(A1:B1)*0.075,2)")]]))
        self.assertEqual(plan.sheets[0].cells["C1"].cache, Decimal("1.50"))

    def test_closed_function_reference_and_typed_cell_boundaries(self):
        for expression in ('=DDE("cmd","topic","item")', "=CALL(1)", "=EXEC(1)", "=_xlfn.WEBSERVICE(1)",
                           "=OFFSET(A1,1,1)", "=SUM(A1#)", "=SUM(A:A)", "=Sheet1:Sheet3!A1",
                           "='[outside.xlsx]Data'!A1", "=cmd|' /C calc'!A1", "=SUM.__call__(1)", "=__import__(1)"):
            with self.subTest(expression=expression), self.assertRaises(formulas.SpreadsheetError):
                formulas.prepare_workbook(workbook([[fn(expression)]]))
        for cell in ({"type": "formula", "expression": "=1", "cache": 2}, {"type": "formula", "expression": "=1", "ast": ["number", 2]},
                     {"type": "number", "value": "2", "formula": "=9"}):
            with self.assertRaises(formulas.SpreadsheetError): formulas.prepare_workbook(workbook([[cell]]))

    def test_unicode_uppercase_alias_never_survives_as_different_emitted_function(self):
        for expression in ("=ſUM(1,2)", "=MıN(1,2)"):
            try:
                plan = formulas.prepare_workbook(workbook([[fn(expression)]]))
            except formulas.SpreadsheetError:
                continue
            self.assertTrue(plan.sheets[0].cells["A1"].formula.isascii())

    def test_real_range_reference_and_depth_limits_and_multisheet_cycle(self):
        for expression in ("=SUM(B1:B10001)", "=" + "+".join("1" for _ in range(200)),
                           "=" + "+".join("SUM(B1:B10000)" for _ in range(10))):
            with self.subTest(length=len(expression)), self.assertRaises(formulas.SpreadsheetError):
                formulas.prepare_workbook(workbook([[fn(expression)]]))
        with self.assertRaisesRegex(formulas.SpreadsheetError, "FORMULA_CYCLE"):
            formulas.prepare_workbook([{"name": "A", "rows": [[fn("=B!A1")]]},
                {"name": "B", "rows": [[fn("=SUM(C!A1:A2)")]]}, {"name": "C", "rows": [[fn("=A!A1")], [2]]}])

    def test_cumulative_unicode_input_budget_is_global_and_names_are_unambiguous(self):
        with self.assertRaisesRegex(formulas.SpreadsheetError, "SPREADSHEET_INPUT_LIMIT"):
            formulas.prepare_workbook([{"name": "Budget" + str(i), "rows": [["鲸" * 30000, "鲸" * 30000]]} for i in range(32)])
        for names in (("Data", "data"), ("'Data", "Other"), ("Data'", "Other")):
            with self.assertRaises(formulas.SpreadsheetError):
                formulas.prepare_workbook([{"name": name, "rows": [[1]]} for name in names])


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "XLSX producer unavailable")
class FormulaPackageReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xlsx-independent-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.writer = documents.DocumentTools(self.root)
        self.spec = {"sheets": workbook([[num("19.95", "0.00"), num("0.05", "0.00"), fn("=ROUND(SUM(A1:B1)*0.075,2)", "0.00")]])}
        self.writer.create_document("valid.xlsx", "xlsx", self.spec)
        self.raw = (self.root / "valid.xlsx").read_bytes()
        self.plan = formulas.prepare_workbook(self.spec["sheets"])

    def cell_mutant(self, callback, address="C1"):
        def change(root): callback(next(cell for cell in root.iter(S + "c") if cell.get("r") == address))
        return mutate(self.raw, {"xl/worksheets/sheet1.xml": change})

    def test_duplicate_formula_nodes_cannot_smuggle_a_forbidden_function(self):
        raw = self.cell_mutant(lambda cell: setattr(ET.SubElement(cell, S + "f"), "text", 'HYPERLINK("https://example.invalid","bad")'))
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(raw, self.plan)

    def test_duplicate_cache_nodes_are_rejected_instead_of_trusting_the_first(self):
        raw = self.cell_mutant(lambda cell: setattr(ET.SubElement(cell, S + "v"), "text", "999"))
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(raw, self.plan)

    def test_custom_format_cannot_redefine_a_reserved_builtin_id(self):
        def styles(root):
            formats = root.find(S + "numFmts")
            if formats is None: formats = ET.SubElement(root, S + "numFmts")
            ET.SubElement(formats, S + "numFmt", {"numFmtId": "0", "formatCode": "0.00"})
            formats.set("count", str(len(formats)))
        raw = mutate(self.cell_mutant(lambda cell: cell.set("s", "0")), {"xl/styles.xml": styles})
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(raw, self.plan)

    def test_cache_type_formula_attributes_and_style_indices_cannot_disagree(self):
        changes = [lambda cell: setattr(cell.find(S + "v"), "text", "1.51"),
                   lambda cell: cell.set("t", "str"), lambda cell: cell.set("s", "-1"),
                   lambda cell: cell.set("s", "99999"), lambda cell: cell.find(S + "f").set("t", "shared"),
                   lambda cell: setattr(ET.SubElement(ET.SubElement(cell, S + "is"), S + "t"), "text", "hidden")]
        for change in changes:
            with self.subTest(mutant=changes.index(change)), self.assertRaises(formulas.SpreadsheetError):
                formulas.verify_xlsx(self.cell_mutant(change), self.plan)

    def test_style_count_and_duplicate_custom_format_definitions_fail_closed(self):
        def wrong_count(root): root.find(S + "cellXfs").set("count", "999")
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(mutate(self.raw, {"xl/styles.xml": wrong_count}), self.plan)
        def duplicate(root):
            formats = root.find(S + "numFmts")
            if formats is None: formats = ET.SubElement(root, S + "numFmts")
            for _ in range(2): ET.SubElement(formats, S + "numFmt", {"numFmtId": "164", "formatCode": "0.000"})
            formats.set("count", str(len(formats)))
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(mutate(self.raw, {"xl/styles.xml": duplicate}), self.plan)

    def test_external_relationship_and_entity_xml_are_rejected(self):
        def external(root): ET.SubElement(root, "Relationship", {"Id": "qa-external", "Target": "https://example.invalid", "TargetMode": "External", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"})
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(mutate(self.raw, {"xl/_rels/workbook.xml.rels": external}), self.plan)
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.raw)) as original, zipfile.ZipFile(output, "w") as result:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml": content = b'<!DOCTYPE worksheet [<!ENTITY x "QA">]>' + content
                result.writestr(item, content)
        with self.assertRaises(formulas.SpreadsheetError): formulas.verify_xlsx(output.getvalue(), self.plan)

    def test_hand_calculated_cross_sheet_invoice_has_exact_xml_caches(self):
        sheets = [{"name": "O'Brien 价格", "columns": ["Unit", "Qty"], "rows": [[num("12.345"), 3], [num("0.055"), 2], [num("-1.005"), 1]]},
            {"name": "结算", "rows": [[fn("=ROUND('O''Brien 价格'!A2*'O''Brien 价格'!B2+'O''Brien 价格'!A3*'O''Brien 价格'!B3+'O''Brien 价格'!A4,2)", "0.00"),
                fn("=ROUND(A1*0.075,2)", "0.00"), fn("=SUM(A1:B1)", "#,##0.00"), fn("=ROUND(-1.005,2)", "0.00")]]}]
        result = json.loads(self.writer.create_document("invoice.xlsx", "xlsx", {"sheets": sheets}))
        with zipfile.ZipFile(self.root / "invoice.xlsx") as archive:
            root = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
            cells = {cell.get("r"): cell for cell in root.iter(S + "c")}
            self.assertEqual({key: Decimal(cells[key].findtext(S + "v")) for key in ("A1", "B1", "C1", "D1")},
                {"A1": Decimal("36.14"), "B1": Decimal("2.71"), "C1": Decimal("38.85"), "D1": Decimal("-1.01")})
            self.assertIn("'O''Brien 价格'!", cells["A1"].findtext(S + "f"))
            styles = ET.fromstring(archive.read("xl/styles.xml"))
            formats = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00"}
            formats.update({int(cell.get("numFmtId")): cell.get("formatCode") for cell in styles.findall(S + "numFmts/" + S + "numFmt")})
            xfs = list(styles.find(S + "cellXfs"))
            self.assertEqual(formats[int(xfs[int(cells["C1"].get("s", "0"))].get("numFmtId"))], "#,##0.00")
        self.assertFalse(result["verification"]["spreadsheet_calculation"]["excel_recalculated"])

    def test_literal_injection_strings_boolean_blank_and_float_wire_value(self):
        literal = ['=WEBSERVICE("https://example.invalid")', "=cmd|' /C calc'!A1", "+1+2", "@SUM(A1)"]
        spec = {"sheets": workbook([[*literal], [1.0000000000000002, fn("=A2-1"), True, None], ["4", num("0.125", "0.0%"), fn("=COUNT(A3:B3)")]])}
        self.writer.create_document("literals.xlsx", "xlsx", spec)
        with zipfile.ZipFile(self.root / "literals.xlsx") as archive:
            root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml")); cells = {cell.get("r"): cell for cell in root.iter(S + "c")}
            for address, expected in zip(("A1", "B1", "C1", "D1"), literal):
                self.assertEqual(cells[address].get("t"), "inlineStr")
                self.assertIsNone(cells[address].find(S + "f"))
                self.assertEqual("".join(item.text or "" for item in cells[address].iter(S + "t")), expected)
            self.assertEqual(Decimal(cells["A2"].findtext(S + "v")), Decimal(1))
            self.assertEqual(Decimal(cells["B2"].findtext(S + "v")), Decimal(0))
            self.assertEqual(cells["C2"].get("t"), "b")
            self.assertEqual(Decimal(cells["B3"].findtext(S + "v")), Decimal("0.125"))
            self.assertEqual(cells["C3"].findtext(S + "v"), "1")

    def test_wrong_expected_hash_and_late_verifier_failure_never_replace_target(self):
        target = self.root / "valid.xlsx"
        original = target.read_bytes(); digest = hashlib.sha256(original).hexdigest()
        self.writer.read_document("valid.xlsx")
        with self.assertRaises(ValueError): self.writer.create_document("valid.xlsx", "xlsx", self.spec, overwrite=True, expected_hash="0" * 64)
        self.assertEqual(target.read_bytes(), original)
        with patch.object(documents, "cache_and_verify_xlsx", side_effect=formulas.SpreadsheetError("SPREADSHEET_READBACK_FAILED")):
            with self.assertRaises(documents.DocumentError): self.writer.create_document("valid.xlsx", "xlsx", self.spec, overwrite=True, expected_hash=digest)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual([path.name for path in self.root.iterdir()], ["valid.xlsx"])


if __name__ == "__main__":
    unittest.main()
