"""Deterministic decimal/CSV tool outcomes, without a model or user files."""
from __future__ import annotations

import csv
from decimal import Decimal, getcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_tools import (
    CALCULATION_PRECISION, DataToolError, DataTools, MAX_AST_DEPTH,
    MAX_CSV_GROUPS, MAX_CSV_ROWS, MAX_EXPRESSION_CHARACTERS,
)
from safe_files import MAX_FILE_BYTES


class DataToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-data-tools-")
        self.root = Path(self.directory.name)
        self.tools = DataTools(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def calc(self, text):
        return json.loads(self.tools.calculate(text))

    def csv(self, rows, *, header=("类别", "金额"), name="amounts.csv", delimiter=","):
        path = self.root / name
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=delimiter)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def analyze(self, name="amounts.csv", **kwargs):
        return json.loads(self.tools.analyze_csv(name, "金额", "类别", **kwargs))

    def assert_code(self, code, operation):
        with self.assertRaises(DataToolError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_decimal_literals_are_exact_instead_of_binary_floats(self):
        result = self.calc("0.1 + 0.2")
        self.assertEqual(result["result"], "0.3")
        self.assertTrue(result["exact"])
        self.assertIsInstance(result["result"], str)
        self.assertTrue(result["read_only"])
        self.assertEqual(self.calc("1.000000000000000000000000000001 - 1")["result"],
                         "0.000000000000000000000000000001")

    def test_precedence_negative_values_exponents_and_zero(self):
        for expression, expected in (("2 + 3 * 4", "14"), ("(2+3)*4", "20"),
                                     ("-5 + +2", "-3"), ("2**-3", "0.125"),
                                     ("1e3*.005", "5"), ("-0.0", "0")):
            with self.subTest(expression=expression):
                self.assertEqual(self.calc(expression)["result"], expected)

    def test_floor_division_and_modulo_have_consistent_mathematical_signs(self):
        for expression, expected in (("-5//2", "-3"), ("-5%2", "1"),
                                     ("5//-2", "-3"), ("5%-2", "-1"),
                                     ("5.5%2", "1.5"), ("-5.5//2", "-3")):
            with self.subTest(expression=expression):
                self.assertEqual(self.calc(expression)["result"], expected)

    def test_supported_functions_and_documented_half_even_rounding(self):
        for expression, expected in (("sum([0.1,0.2,0.3])", "0.6"),
                                     ("sum(1,2,3)", "6"), ("min(3, -2, 7)", "-2"),
                                     ("max((3,4,1))", "4"), ("abs(-9)", "9"),
                                     ("round(1.005,2)", "1"), ("round(1.015,2)", "1.02")):
            with self.subTest(expression=expression):
                result = self.calc(expression)
                self.assertEqual(result["result"], expected)
                self.assertEqual(result["rounding"], "ROUND_HALF_EVEN")

    def test_repeating_result_is_explicitly_not_exact(self):
        result = self.calc("1/3")
        self.assertFalse(result["exact"])
        self.assertEqual(result["precision"], CALCULATION_PRECISION)
        self.assertEqual(result["result"], "0." + "3" * CALCULATION_PRECISION)
        self.assertTrue(self.calc("1/8")["exact"])

    def test_calculation_does_not_mutate_process_decimal_context(self):
        before = (getcontext().prec, getcontext().rounding, dict(getcontext().flags))
        self.calc("1/3")
        self.assertEqual((getcontext().prec, getcontext().rounding, dict(getcontext().flags)), before)

    def test_python_code_objects_and_names_are_never_executed(self):
        marker = self.root / "must-not-exist"
        expressions = ["__import__('os').system('echo unsafe')", "open('must-not-exist','w')",
                       "(1).__class__", "[x for x in range(9)]", "lambda: 1", "True", "1j",
                       "0xff", "1_000", "x", "sum(x=1)", "[1,2]", "'x'*200", "f'{1}'",
                       "sum([[1]])", "min()", "abs(1,2)"]
        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(DataToolError):
                    self.calc(expression)
        self.assertFalse(marker.exists())

    def test_invalid_syntax_division_and_nonfinite_inputs_have_static_errors(self):
        for expression in ("1/0", "1//0", "1%0"):
            with self.subTest(expression=expression):
                self.assert_code("DIVISION_BY_ZERO", lambda: self.calc(expression))
        for expression in ("", " ", "1+", "("):
            with self.subTest(expression=expression):
                self.assert_code("INVALID_EXPRESSION", lambda: self.calc(expression))
        for expression in ("NaN", "Infinity", "1e9999", "9"*61, "2**99999999", "2**0.5"):
            with self.subTest(expression=expression):
                with self.assertRaises(DataToolError):
                    self.calc(expression)

    def test_expression_size_depth_and_function_value_budgets_are_bounded(self):
        for expression in ("1" * (MAX_EXPRESSION_CHARACTERS+1),
                           "+" * (MAX_AST_DEPTH+2) + "1", "sum(" + ",".join("1" for _ in range(101)) + ")",
                           "+".join("1" for _ in range(100))):
            with self.subTest(prefix=expression[:20]):
                self.assert_code("CALCULATION_LIMIT", lambda: self.calc(expression))

    def test_cooperative_calculation_deadline_is_checked(self):
        with patch("data_tools.time.monotonic", side_effect=[0.0, 2.0]):
            self.assert_code("CALCULATION_LIMIT", lambda: self.calc("1+2"))

    def test_original_20_row_regression_is_exactly_1233_39(self):
        rows = [("设备", "129.90"), ("资料", "35.60"), ("交通", "18.00"), ("设备", "289.00"),
                ("资料", "67.80"), ("交通", "12.50"), ("设备", "48.75"), ("资料", "9.99"),
                ("交通", "21.40"), ("设备", "199.00"), ("资料", "105.30"), ("交通", "8.80"),
                ("设备", "36.66"), ("资料", "12.34"), ("交通", "3.20"), ("设备", "88.88"),
                ("资料", "45.67"), ("交通", "19.90"), ("设备", "57.25"), ("资料", "23.45")]
        path = self.csv(rows)
        before = path.read_bytes()
        result = self.analyze()
        self.assertEqual(result["total"], "1233.39")
        self.assertEqual(result["category_totals"], {"设备": "849.44", "资料": "300.15", "交通": "83.8"})
        self.assertEqual(result["record_count"], 20)
        self.assertEqual(Decimal(result["total"]), sum(Decimal(value) for value in result["category_totals"].values()))
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["source_bytes"], len(before))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(result["exact"])

    def test_thousands_cny_prefix_suffix_and_accounting_negatives(self):
        self.csv([("A", "￥1,234.50"), ("A", "(¥20.10)"), ("B", "10.60元"),
                  ("B", "CNY 1")])
        result = self.analyze()
        self.assertEqual(result["total"], "1226")
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["warnings"], [])

    def test_supported_marked_currencies_and_mixed_currency_rejection(self):
        for symbol, code in (("$", "USD"), ("€", "EUR"), ("£", "GBP")):
            with self.subTest(code=code):
                self.csv([("A", symbol+"1,200.30"), ("A", "("+symbol+"0.30)")])
                result = self.analyze()
                self.assertEqual(result["total"], "1200")
                self.assertEqual(result["currency"], code)
        self.csv([("A", "$10"), ("A", "￥20")])
        self.assert_code("MIXED_CURRENCIES", self.analyze)

    def test_unmarked_values_are_not_silently_assigned_a_currency(self):
        self.csv([("A", "￥10"), ("A", "20")])
        result = self.analyze()
        self.assertEqual(result["total"], "30")
        self.assertIsNone(result["currency"])
        self.assertEqual(result["currency_labels"], ["CNY"])
        self.assertEqual(result["unmarked_currency_rows"], 1)
        self.assertIn("UNMARKED_CURRENCY_VALUES", result["warnings"])

    def test_signed_decimal_and_exponent_values(self):
        self.csv([("A", ".50"), ("A", "-0.1"), ("A", "1e2"), ("A", "+2.25")])
        self.assertEqual(self.analyze()["total"], "102.65")

    def test_ambiguous_or_malformed_numeric_rows_are_errors_not_silently_skipped(self):
        for value in ("1,23", "1.234,56", "NaN", "inf", "", "未知", "1 234", "(-2)", "($-2)", "1e9999"):
            with self.subTest(value=value):
                self.csv([("A", "10"), ("B", value)])
                exc = self.assert_code("CSV_NUMBER", self.analyze)
                self.assertEqual(exc.line, 3)
                self.assertNotIn(value, str(exc)) if value not in ("", "1") else None

    def test_blank_records_are_counted_and_header_only_is_explicitly_empty(self):
        (self.root / "amounts.csv").write_text("类别,金额\n\n,\n A , 1.20 \n", encoding="utf-8")
        result = self.analyze()
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["blank_records_skipped"], 2)
        self.assertEqual(result["category_totals"], {"A": "1.2"})
        self.csv([])
        result = self.analyze()
        self.assertTrue(result["empty"])
        self.assertEqual(result["total"], "0")
        self.assertEqual(result["record_count"], 0)

    def test_missing_columns_duplicate_headers_and_invalid_row_shapes(self):
        for text, code in (("", "CSV_HEADER"), ("金额,金额\n1,2", "CSV_HEADER"),
                           ("类别,\nA,1", "CSV_HEADER"), ("分类,金额\nA,1", "CSV_COLUMN"),
                           ("类别,金额\nA,1,extra", "CSV_FORMAT"),
                           ('类别,金额\nA,"unfinished', "CSV_FORMAT"),
                           ("类别,金额\nA,1\0", "CSV_FORMAT")):
            with self.subTest(text=text[:20]):
                (self.root / "amounts.csv").write_text(text, encoding="utf-8")
                self.assert_code(code, self.analyze)

    def test_explicit_semicolon_delimiter_and_no_grouping(self):
        self.csv([("A", "1"), ("B", "2.5")], delimiter=";")
        result = json.loads(self.tools.analyze_csv("amounts.csv", "金额", delimiter=";"))
        self.assertEqual(result["total"], "3.5")
        self.assertEqual(result["category_totals"], {})
        self.assertEqual(result["group_by"], "")

    def test_group_and_row_caps_fail_instead_of_claiming_a_partial_total(self):
        self.csv([(str(index), "1") for index in range(MAX_CSV_GROUPS+1)])
        self.assert_code("CSV_LIMIT", self.analyze)
        self.csv([("A", "1")]*(MAX_CSV_ROWS+1))
        self.assert_code("CSV_LIMIT", self.analyze)

    def test_decimal_sum_keeps_widely_different_scales_exact(self):
        self.csv([("A", "1e100"), ("A", "0.01")])
        result = self.analyze()
        self.assertEqual(result["total"], "1" + "0"*100 + ".01")
        self.assertTrue(result["exact"])

    def test_file_size_utf8_and_missing_file_errors(self):
        path = self.root / "amounts.csv"
        path.write_bytes(b"x"*(MAX_FILE_BYTES+1))
        self.assert_code("FILE_UNAVAILABLE", self.analyze)
        path.write_bytes(b"\xff\xfe\x00")
        self.assert_code("CSV_ENCODING", self.analyze)
        self.assert_code("FILE_UNAVAILABLE", lambda: self.analyze("missing.csv"))

    def test_parent_absolute_and_link_escape_are_blocked(self):
        with tempfile.TemporaryDirectory(prefix="aura-data-outside-") as directory:
            outside = Path(directory) / "private.csv"
            outside.write_text("类别,金额\nA,1", encoding="utf-8")
            self.assert_code("OUTSIDE_WORKSPACE", lambda: self.analyze(str(outside)))
            self.assert_code("OUTSIDE_WORKSPACE", lambda: self.analyze("../private.csv"))
            link = self.root / "escape.csv"
            try:
                os.symlink(outside, link)
            except OSError:
                if os.name != "nt":
                    self.skipTest("OS does not allow unprivileged link creation")
                # NTFS junctions need no symlink privilege. Only create this
                # owned temporary link; rmdir below removes the link itself.
                junction = self.root / "outside-junction"
                result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside.parent)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode != 0:
                    self.skipTest("OS does not allow temporary symlinks or junctions")
                try:
                    self.assert_code("OUTSIDE_WORKSPACE", lambda: self.analyze("outside-junction/private.csv"))
                finally:
                    os.rmdir(junction)
            else:
                self.assert_code("OUTSIDE_WORKSPACE", lambda: self.analyze("escape.csv"))

    def test_invalid_options_and_csv_deadline_are_static(self):
        self.csv([("A", "1")])
        for delimiter in ("", "||", "\n", '"'):
            with self.subTest(delimiter=delimiter):
                self.assert_code("INVALID_CSV_OPTIONS", lambda: self.analyze(delimiter=delimiter))
        with patch("data_tools.time.monotonic", side_effect=[0.0, 2.0]):
            self.assert_code("CSV_LIMIT", self.analyze)


if __name__ == "__main__":
    unittest.main()
