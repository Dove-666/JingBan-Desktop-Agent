"""Independent benchmark ZIP/CSV oracle positives and adversarial negatives."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aura_xlsx_formula_oracle", ROOT / "benchmarks/agent_tasks/xlsx_formula_oracle.py")
oracle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oracle)


class XlsxFormulaBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-formula-bench-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / oracle.SOURCE).write_bytes(oracle.SOURCE_TEXT.encode("utf-8"))

    def reference(self):
        oracle.write_reference_outputs(self.root, oracle.CASE)
        return (self.root / oracle.OUTPUT).read_bytes()

    def rewrite(self, raw, change):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist(): target.writestr(item, change(item.filename, source.read(item.filename)))
        (self.root / oracle.OUTPUT).write_bytes(output.getvalue())

    def failed(self, code=None):
        result = oracle.evaluate(self.root, oracle.CASE)
        self.assertFalse(result["passed"], result)
        self.assertTrue(result["issues"], result)
        if code: self.assertEqual([code], result["issues"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_catalog_is_fixed_narrow_chinese_task_and_does_not_rewrite_v2_prefix(self):
        task = oracle.catalog_entry()
        self.assertEqual("spreadsheets_formula_subset", task["category"])
        self.assertEqual([oracle.SOURCE], [item["path"] for item in task["fixtures"]])
        for text in ("ROUND(B2*C2,2)", "SUM(D2:D4)", "0.000", "0.00", "普通文本", "source.csv"):
            self.assertIn(text, task["prompt"])
        self.assertNotIn("xlsx_formula_oracle", json.dumps(task))
        self.assertNotIn("reference_package", json.dumps(task))
        task["oracles"][0]["path"] = "changed.xlsx"
        self.assertEqual(oracle.OUTPUT, oracle.catalog_entry()["oracles"][0]["path"])
        catalog = json.loads((ROOT / "benchmarks/agent_tasks/catalog.json").read_bytes())
        prefix = catalog["tasks"][:29]
        digest = hashlib.sha256(json.dumps(prefix, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual("18fe8fe554b2b32f5305e85fa71a158eb7cef2f5d0b38fe915726662b07385a6", digest)

    def test_validate_accepts_only_closed_case_fields_and_fixed_paths(self):
        good = oracle.catalog_entry()["oracles"][0]
        self.assertTrue(oracle.validate(good))
        changes = [{"case": "arbitrary_excel"}, {"path": "../budget.xlsx"}, {"source_path": "private.csv"},
                   {"kind": "xlsx_cells"}, {"recalculate": "excel"}, {"case": True}]
        for change in changes:
            bad = deepcopy(good); bad.update(change)
            with self.assertRaisesRegex(oracle.XlsxOracleError, "XLSX_ORACLE_INVALID_SPEC"): oracle.validate(bad)
        bad = deepcopy(good); bad.pop("case")
        with self.assertRaises(oracle.XlsxOracleError): oracle.validate(bad)
        self.assertEqual(["XLSX_ORACLE_UNKNOWN_CASE"], oracle.evaluate(self.root, "wrong")["issues"])

    def test_minimal_standard_reference_passes_independent_csv_derived_cache_and_formats(self):
        raw = self.reference()
        result = oracle.evaluate(self.root, oracle.CASE)
        self.assertTrue(result["passed"], result)
        self.assertEqual(oracle.SCOPE, result["scope"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["artifact_sha256"])
        self.assertEqual(hashlib.sha256(oracle.SOURCE_TEXT.encode()).hexdigest(), result["source_sha256"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = {cell.get("r"): cell for cell in root.findall(f".//{{{oracle.S}}}c")}
            self.assertEqual(["59.99", "14.47", "0.53", "74.99"], [cells[name].findtext(f"{{{oracle.S}}}v") for name in ("D2", "D3", "D4", "D5")])
            self.assertEqual("SUM(D2:D4)", cells["D5"].findtext(f"{{{oracle.S}}}f"))
            self.assertIsNone(cells["B6"].find(f"{{{oracle.S}}}f"))

    def test_five_required_mutants_fail_independently_and_selfchecks_are_repeatable(self):
        raw = self.reference()
        for name in ("wrong_cache", "formula_to_literal", "wrong_format", "literal_to_formula", "external_relationship"):
            (self.root / oracle.OUTPUT).write_bytes(oracle._mutant(raw, name))
            self.failed()
        for _ in range(2):
            report = oracle.oracle_selfchecks()
            self.assertTrue(report["passed"], report)
            self.assertEqual(0, report["model_calls"])
            self.assertFalse(report["real_model_success"])
            self.assertEqual(6, len(report["checks"]))

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "optional independent workbook reader")
    def test_minimal_reference_is_readable_as_standard_editable_workbook_and_numeric_cache(self):
        from openpyxl import load_workbook
        raw = self.reference()
        workbook = load_workbook(io.BytesIO(raw), data_only=False)
        try:
            self.assertEqual([oracle.SHEET], workbook.sheetnames)
            self.assertEqual("=ROUND(B2*C2,2)", workbook[oracle.SHEET]["D2"].value)
            self.assertEqual("0.000", workbook[oracle.SHEET]["C2"].number_format)
            self.assertEqual("s", workbook[oracle.SHEET]["B6"].data_type)
            self.assertEqual(oracle.LITERAL, workbook[oracle.SHEET]["B6"].value)
        finally: workbook.close()
        workbook = load_workbook(io.BytesIO(raw), data_only=True)
        try:
            self.assertEqual(59.99, workbook[oracle.SHEET]["D2"].value)
            self.assertEqual(74.99, workbook[oracle.SHEET]["D5"].value)
        finally: workbook.close()

    def test_preserved_total_does_not_hide_wrong_source_cells_or_wrong_formula(self):
        raw = self.reference()
        for address, tag, text, expected in (("C2", "v", "20.000", "XLSX_ORACLE_NUMERIC_CACHE_MISMATCH"),
                                             ("D2", "f", "ROUND(B3*C3,2)", "XLSX_ORACLE_FORMULA_MISMATCH")):
            def change(name, content):
                if name == "xl/worksheets/sheet1.xml":
                    root = ET.fromstring(content)
                    cell = next(cell for cell in root.findall(f".//{{{oracle.S}}}c") if cell.get("r") == address)
                    cell.find(f"{{{oracle.S}}}{tag}").text = text
                    return ET.tostring(root)
                return content
            self.rewrite(raw, change); self.failed(expected)

    def test_source_mutation_or_missing_artifact_fails_without_raw_paths(self):
        self.failed("XLSX_ORACLE_FILE_UNAVAILABLE")
        self.reference()
        (self.root / oracle.SOURCE).write_bytes(oracle.SOURCE_TEXT.encode() + b"\n")
        self.failed("XLSX_ORACLE_SOURCE_CHANGED")

    def test_duplicate_zip_members_declared_expansion_and_unsafe_xml_are_rejected(self):
        raw = self.reference()
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive, zipfile.ZipFile(output, "w") as target:
            for item in archive.infolist(): target.writestr(item, archive.read(item.filename))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                target.writestr("xl/workbook.xml", b"duplicate")
        (self.root / oracle.OUTPUT).write_bytes(output.getvalue())
        self.failed("XLSX_ORACLE_UNSAFE_PACKAGE")
        (self.root / oracle.OUTPUT).write_bytes(raw)
        with patch.object(oracle, "MAX_EXPANDED", 10): self.failed("XLSX_ORACLE_UNSAFE_PACKAGE")
        def dtd(name, content):
            if name == "xl/worksheets/sheet1.xml": return b'<!DOCTYPE worksheet [<!ENTITY leak "BAD">]>' + content
            return content
        self.rewrite(raw, dtd); self.failed("XLSX_ORACLE_UNSAFE_XML")
        def utf16(name, content):
            if name == "xl/worksheets/sheet1.xml": return ('<!DOCTYPE worksheet [<!ENTITY leak "BAD">]>' + content.decode()).encode("utf-16")
            return content
        self.rewrite(raw, utf16); self.failed("XLSX_ORACLE_UNSAFE_XML")

    def test_duplicate_formula_cache_and_reserved_style_redefinition_are_rejected(self):
        raw = self.reference()
        for child in ("f", "v"):
            def duplicate(name, content):
                if name == "xl/worksheets/sheet1.xml":
                    root = ET.fromstring(content)
                    cell = next(cell for cell in root.findall(f".//{{{oracle.S}}}c") if cell.get("r") == "D2")
                    ET.SubElement(cell, f"{{{oracle.S}}}{child}").text = "999" if child == "v" else "SUM(1,2)"
                    return ET.tostring(root)
                return content
            self.rewrite(raw, duplicate); self.failed()
        def reserved(name, content):
            if name == "xl/styles.xml":
                root = ET.fromstring(content)
                ET.SubElement(root.find(f"{{{oracle.S}}}numFmts"), f"{{{oracle.S}}}numFmt", numFmtId="0", formatCode="0.00")
                return ET.tostring(root)
            return content
        self.rewrite(raw, reserved); self.failed("XLSX_ORACLE_INVALID_STYLES")

    def test_hardlink_or_symlink_output_cannot_escape_owned_workspace(self):
        self.reference()
        outside = self.root.parent / (self.root.name + "-outside.xlsx")
        outside.write_bytes((self.root / oracle.OUTPUT).read_bytes())
        self.addCleanup(outside.unlink, missing_ok=True)
        (self.root / oracle.OUTPUT).unlink()
        os.link(outside, self.root / oracle.OUTPUT)
        self.failed("XLSX_ORACLE_WORKSPACE_ESCAPE")

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "actual producer test requires optional openpyxl")
    def test_actual_document_tool_output_passes_separate_stdlib_benchmark_oracle(self):
        backend = ROOT / "agent_backend/nanobot"
        sys.path.insert(0, str(backend))
        import document_tools
        rows, _ = oracle._reference(oracle.SOURCE_TEXT.encode())
        body = []
        for row_number, (item, quantity, price, _) in enumerate(rows, 2):
            body.append([item, {"type": "number", "value": str(quantity), "number_format": "0"},
                {"type": "number", "value": str(price), "number_format": "0.000"},
                {"type": "formula", "expression": "=" + oracle._FORMULAS[f"D{row_number}"], "number_format": "0.00"}])
        body.extend([["合计", None, None, {"type": "formula", "expression": "=SUM(D2:D4)", "number_format": "0.00"}],
                     ["字面示例", oracle.LITERAL]])
        document_tools.DocumentTools(self.root).create_document(oracle.OUTPUT, "xlsx",
            {"sheets": [{"name": oracle.SHEET, "columns": ["商品", "数量", "单价", "金额"], "rows": body}]})
        self.assertTrue(oracle.evaluate(self.root, oracle.CASE)["passed"])


if __name__ == "__main__":
    unittest.main()
