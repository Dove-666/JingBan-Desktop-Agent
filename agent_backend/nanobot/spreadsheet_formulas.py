"""Bounded Decimal formula DSL and independent XLSX cache readback.

No eval, Python/Excel execution, file IO, macros, names, external references or
provider calls. Ordinary strings stay literal; only closed typed cells opt in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, Context, localcontext, ROUND_HALF_EVEN, ROUND_HALF_UP
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile


PRECISION = 34
MAX_CELLS = 50_000
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_FORMULAS = 4096
MAX_FORMULA_LENGTH = 4096
MAX_AST_NODES = 1024
MAX_WORKBOOK_NODES = 100_000
MAX_REFERENCES = 100_000
MAX_RANGE_CELLS = 10_000
MAX_DEPTH = 64
MAX_ROW = 10_001
MAX_COLUMN = 100
MAX_ADJUSTED = 300
FUNCTIONS = frozenset({"SUM", "AVERAGE", "COUNT", "MIN", "MAX", "ROUND"})
NUMBER_FORMATS = frozenset({"General", "0", "#,##0", "0%"} |
    {prefix + "." + "0" * places + suffix for prefix in ("0", "#,##0") for places in range(1, 7) for suffix in ("", "%")})
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z")
_IDENTIFIER = re.compile(r"[^\W\d]\w*", re.UNICODE)
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_BUILTIN_FORMATS = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%"}


class SpreadsheetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise SpreadsheetError(code) from None


def _text(value, maximum):
    if (type(value) is not str or len(value) > maximum or any(
        ord(char) < 32 and char not in "\t\n\r" or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF)
        for char in value)):
        _fail("INVALID_DOCUMENT_TEXT")
    return value


def _number(value, *, exact=False):
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        parts = result.as_tuple()
        digits = list(parts.digits)
        while len(digits) > 1 and digits[-1] == 0: digits.pop()
        if (not result.is_finite() or abs(parts.exponent) > MAX_ADJUSTED or
            not result.is_zero() and abs(result.adjusted()) > MAX_ADJUSTED or exact and len(digits) > 15):
            _fail("FORMULA_NUMBER_LIMIT")
        return result
    except (DecimalException, ValueError, TypeError, OverflowError):
        _fail("FORMULA_NUMBER_LIMIT")


def decimal_text(value):
    if value.is_zero(): return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def coordinate(column, row):
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters + str(row)


@dataclass
class Cell:
    kind: str
    value: object
    number_format: str = "General"
    formula: str | None = None
    ast: tuple | None = None
    cache: Decimal | None = None


@dataclass
class Sheet:
    name: str
    rows: list
    has_columns: bool
    cells: dict = field(default_factory=dict)


@dataclass
class WorkbookPlan:
    sheets: list
    formula_count: int
    typed_number_count: int

    def verification(self):
        return {"formula_cells": self.formula_count, "typed_number_cells": self.typed_number_count,
            "cache_source": "host_decimal_evaluator_v1", "decimal_precision": PRECISION,
            "formula_and_cache_xml_verified": True, "number_formats_verified": True,
            "excel_recalculated": False, "visual_layout_verified": False, "recalculation_requested_on_open": True}


def _cell(value):
    if type(value) is dict:
        kind = value.get("type")
        required = {"type", "expression"} if kind == "formula" else {"type", "value"} if kind == "number" else None
        if required is None or not required <= set(value) or set(value) - required - {"number_format"}:
            _fail("INVALID_TYPED_CELL")
        formatting = value.get("number_format", "General")
        if type(formatting) is not str or formatting not in NUMBER_FORMATS:
            _fail("INVALID_CELL_NUMBER_FORMAT")
        if kind == "number":
            text = value["value"]
            if type(text) is not str or len(text) > 128 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
                _fail("INVALID_TYPED_NUMBER")
            return Cell("typed_number", _number(text, exact=True), formatting)
        expression = _text(value["expression"], MAX_FORMULA_LENGTH)
        if not expression.startswith("=") or not expression[1:].strip():
            _fail("INVALID_CELL_FORMULA")
        formula = expression[1:].strip()
        return Cell("formula", "=" + formula, formatting, formula=formula)
    if value is None: return Cell("blank", None)
    if type(value) is str: return Cell("string", _text(value, 32767))
    if type(value) is bool: return Cell("boolean", value)
    if type(value) is int:
        if not -999_999_999_999_999 <= value <= 999_999_999_999_999: _fail("CELL_NUMBER_PRECISION_USE_STRING")
        return Cell("number", value, cache=Decimal(value))
    if type(value) is float:
        if not math.isfinite(value): _fail("INVALID_CELL_VALUE")
        # openpyxl's scalar writer uses %.16g. Evaluate the actual stored
        # decimal literal, rather than a different str(float) approximation.
        return Cell("number", value, cache=_number("%.16g" % value))
    _fail("INVALID_CELL_VALUE")


def _tokens(formula):
    result, at = [], 0
    while at < len(formula):
        char = formula[at]
        if char.isspace(): at += 1; continue
        if char in "+-*/(),:!":
            result.append((char, char)); at += 1
        elif char == "'":
            text, at = [], at + 1
            while at < len(formula):
                if formula[at] == "'":
                    if at + 1 < len(formula) and formula[at + 1] == "'": text.append("'"); at += 2; continue
                    at += 1; break
                text.append(formula[at]); at += 1
            else: _fail("FORMULA_SYNTAX")
            result.append(("sheet", "".join(text)))
        else:
            number = _NUMBER.match(formula, at)
            reference = re.match(r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}(?![\w$])", formula[at:])
            identifier = _IDENTIFIER.match(formula, at)
            if number:
                result.append(("number", number.group())); at = number.end()
            elif reference:
                result.append(("word", reference.group())); at += reference.end()
            elif identifier:
                result.append(("word", identifier.group())); at = identifier.end()
            else:
                _fail("FORMULA_UNSUPPORTED_SYNTAX")
        if len(result) > MAX_AST_NODES: _fail("FORMULA_COMPLEXITY_LIMIT")
    return result + [("end", "")]


class Parser:
    def __init__(self, formula, current_sheet, sheets):
        self.tokens, self.at, self.nodes = _tokens(formula), 0, 0
        self.current_sheet, self.sheets = current_sheet, sheets

    def accept(self, kind):
        if self.tokens[self.at][0] == kind:
            token = self.tokens[self.at]; self.at += 1; return token
        return None

    def need(self, kind):
        value = self.accept(kind)
        if value is None: _fail("FORMULA_SYNTAX")
        return value

    def node(self, *values):
        self.nodes += 1
        if self.nodes > MAX_AST_NODES: _fail("FORMULA_COMPLEXITY_LIMIT")
        return values

    def parse(self):
        value = self.expression(0)
        self.need("end")
        return value

    def expression(self, depth):
        if depth > MAX_DEPTH: _fail("FORMULA_COMPLEXITY_LIMIT")
        value = self.product(depth + 1)
        while self.tokens[self.at][0] in ("+", "-"):
            operation = self.tokens[self.at][0]; self.at += 1
            value = self.node(operation, value, self.product(depth + 1))
        return value

    def product(self, depth):
        value = self.primary(depth + 1)
        while self.tokens[self.at][0] in ("*", "/"):
            operation = self.tokens[self.at][0]; self.at += 1
            value = self.node(operation, value, self.primary(depth + 1))
        return value

    def reference(self, first=None, inherited=None):
        token = first or self.tokens[self.at]
        if first is None: self.at += 1
        sheet = inherited or self.current_sheet
        if self.accept("!"):
            sheet_name = token[1].casefold()
            if sheet_name not in self.sheets: _fail("FORMULA_UNKNOWN_SHEET")
            sheet = self.sheets[sheet_name]
            token = self.need("word")
        elif token[0] == "sheet":
            _fail("FORMULA_SYNTAX")
        match = _CELL.fullmatch(token[1])
        if not match: _fail("FORMULA_UNSUPPORTED_REFERENCE")
        column = 0
        for char in match.group(1).upper(): column = column * 26 + ord(char) - 64
        row = int(match.group(2))
        if row > MAX_ROW or column > MAX_COLUMN: _fail("FORMULA_REFERENCE_LIMIT")
        return sheet, column, row

    def primary(self, depth):
        if depth > MAX_DEPTH: _fail("FORMULA_COMPLEXITY_LIMIT")
        if self.accept("+"): return self.node("positive", self.primary(depth + 1))
        if self.accept("-"): return self.node("negative", self.primary(depth + 1))
        if self.accept("("):
            value = self.expression(depth + 1); self.need(")"); return value
        token = self.accept("number")
        if token: return self.node("number", _number(token[1], exact=True))
        token = self.tokens[self.at]
        if token[0] not in ("word", "sheet"): _fail("FORMULA_SYNTAX")
        self.at += 1
        if self.accept("("):
            function = token[1].upper()
            if token[0] != "word" or not token[1].isascii() or function not in FUNCTIONS: _fail("FORMULA_FUNCTION_NOT_ALLOWED")
            args = []
            if not self.accept(")"):
                while True:
                    args.append(self.expression(depth + 1))
                    if len(args) > 64: _fail("FORMULA_COMPLEXITY_LIMIT")
                    if self.accept(")"): break
                    self.need(",")
            if function == "ROUND" and len(args) != 2: _fail("FORMULA_ARGUMENTS")
            return self.node("call", function, tuple(args))
        start = self.reference(token)
        if self.accept(":"):
            if self.tokens[self.at][0] not in ("word", "sheet"): _fail("FORMULA_SYNTAX")
            end = self.reference(inherited=start[0])
            if start[0] != end[0]: _fail("FORMULA_RANGE_SHEET_MISMATCH")
            first_c, last_c = sorted((start[1], end[1])); first_r, last_r = sorted((start[2], end[2]))
            if (last_c - first_c + 1) * (last_r - first_r + 1) > MAX_RANGE_CELLS: _fail("FORMULA_RANGE_LIMIT")
            return self.node("range", start[0], first_c, last_c, first_r, last_r)
        return self.node("reference", *start)


class Evaluator:
    def __init__(self, sheets):
        self.sheets = {sheet.name: sheet for sheet in sheets}
        self.active, self.done = set(), {}
        self.references = self.nodes = 0

    def scalar(self, value):
        if value is None: return Decimal(0)
        if type(value) is list: _fail("FORMULA_RANGE_REQUIRES_AGGREGATE")
        if not isinstance(value, Decimal): _fail("FORMULA_NON_NUMERIC_REFERENCE")
        return value

    def cell(self, sheet, column, row, depth):
        self.references += 1
        if self.references > MAX_REFERENCES or depth > MAX_DEPTH: _fail("FORMULA_EVALUATION_LIMIT")
        key = (sheet, coordinate(column, row))
        if key in self.active: _fail("FORMULA_CYCLE")
        if key in self.done: return self.done[key]
        cell = self.sheets[sheet].cells.get(key[1])
        if cell is None or cell.kind == "blank": return None
        if cell.kind in ("number", "typed_number"): return cell.cache if cell.kind == "number" else cell.value
        if cell.kind != "formula": return cell.value
        self.active.add(key)
        try:
            value = _number(self.scalar(self.evaluate(cell.ast, depth + 1)))
            cell.cache = value
            self.done[key] = value
            return value
        finally:
            self.active.remove(key)

    def evaluate(self, node, depth):
        self.nodes += 1
        if self.nodes > MAX_WORKBOOK_NODES or depth > MAX_DEPTH: _fail("FORMULA_EVALUATION_LIMIT")
        operation = node[0]
        if operation == "number": return node[1]
        if operation == "reference": return self.cell(*node[1:], depth + 1)
        if operation == "range":
            return [self.cell(node[1], column, row, depth + 1) for row in range(node[4], node[5] + 1)
                    for column in range(node[2], node[3] + 1)]
        if operation in ("positive", "negative"):
            value = self.scalar(self.evaluate(node[1], depth + 1))
            return _number(+value if operation == "positive" else -value)
        if operation in ("+", "-", "*", "/"):
            left, right = self.scalar(self.evaluate(node[1], depth + 1)), self.scalar(self.evaluate(node[2], depth + 1))
            if operation == "/" and right.is_zero(): _fail("FORMULA_DIVISION_BY_ZERO")
            return _number(left + right if operation == "+" else left - right if operation == "-" else left * right if operation == "*" else left / right)
        function, args = node[1], node[2]
        if function == "ROUND":
            value, digits = [self.scalar(self.evaluate(arg, depth + 1)) for arg in args]
            if digits != digits.to_integral_value() or not -12 <= digits <= 12: _fail("FORMULA_ROUND_DIGITS")
            return _number(value.quantize(Decimal(1).scaleb(-int(digits)), rounding=ROUND_HALF_UP))
        numbers = []
        for arg in args:
            value = self.evaluate(arg, depth + 1)
            numbers.extend(item for item in (value if type(value) is list else [value]) if isinstance(item, Decimal))
        if function == "COUNT": return Decimal(len(numbers))
        if function == "AVERAGE":
            if not numbers: _fail("FORMULA_DIVISION_BY_ZERO")
            return _number(sum(numbers, Decimal(0)) / len(numbers))
        if function == "SUM": return _number(sum(numbers, Decimal(0)))
        return _number((min(numbers) if function == "MIN" else max(numbers)) if numbers else Decimal(0))


def prepare_workbook(items):
    if type(items) is not list or not 1 <= len(items) <= 32: _fail("INVALID_SHEETS")
    sheets, names, cell_count, formulas, typed_numbers, input_bytes = [], {}, 0, 0, 0, 0
    for item in items:
        if type(item) is not dict: _fail("INVALID_SHEET")
        name = _text(item.get("name", "Sheet"), 31)
        if (not name or name.startswith("'") or name.endswith("'") or name.casefold() in names or
            re.search(r"[\[\]:*?/\\]", name)): _fail("INVALID_SHEET_NAME")
        input_bytes += len(name.encode("utf-8"))
        names[name.casefold()] = name
        columns, rows = item.get("columns", []), item.get("rows", [])
        if type(columns) is not list or len(columns) > MAX_COLUMN or type(rows) is not list or len(rows) > 10_000:
            _fail("INVALID_SHEET_ROWS")
        prepared = Sheet(name, [], bool(columns))
        for row_number, row in enumerate(([columns] if columns else []) + rows, 1):
            if type(row) is not list or len(row) > MAX_COLUMN: _fail("INVALID_SHEET_ROWS")
            cell_count += len(row)
            if cell_count > MAX_CELLS: _fail("SPREADSHEET_CELL_LIMIT")
            values = []
            for value in row:
                cell = _cell(value)
                strings = value.values() if type(value) is dict else (value,)
                input_bytes += sum(len(item.encode("utf-8")) if type(item) is str else 32 for item in strings)
                if input_bytes > MAX_INPUT_BYTES: _fail("SPREADSHEET_INPUT_LIMIT")
                values.append(cell)
            prepared.rows.append(values)
            for column, cell in enumerate(values, 1):
                prepared.cells[coordinate(column, row_number)] = cell
                formulas += cell.kind == "formula"
                typed_numbers += cell.kind == "typed_number"
                if formulas > MAX_FORMULAS: _fail("FORMULA_COUNT_LIMIT")
        sheets.append(prepared)
    total_nodes = 0
    for sheet in sheets:
        for cell in sheet.cells.values():
            if cell.kind == "formula":
                parser = Parser(cell.formula, sheet.name, names)
                cell.ast = parser.parse(); total_nodes += parser.nodes
                # Canonicalize insignificant whitespace without discarding
                # quoted sheet names or absolute-reference dollar signs.
                cell.formula = "".join("'" + value.replace("'", "''") + "'" if kind == "sheet" else value
                                       for kind, value in parser.tokens if kind != "end")
                cell.value = "=" + cell.formula
                if total_nodes > MAX_WORKBOOK_NODES: _fail("FORMULA_COMPLEXITY_LIMIT")
    evaluator = Evaluator(sheets)
    try:
        with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN, Emin=-MAX_ADJUSTED, Emax=MAX_ADJUSTED)):
            for sheet in sheets:
                for address, cell in sheet.cells.items():
                    if cell.kind == "formula":
                        match = _CELL.fullmatch(address); column = 0
                        for char in match.group(1): column = column * 26 + ord(char) - 64
                        evaluator.cell(sheet.name, column, int(match.group(2)), 0)
    except (DecimalException, OverflowError, RecursionError):
        _fail("FORMULA_NUMBER_LIMIT")
    return WorkbookPlan(sheets, formulas, typed_numbers)


def _read_xml(archive, path):
    raw = archive.read(path)
    if len(raw) > 16 * 1024 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper(): _fail("SPREADSHEET_PACKAGE_INVALID")
    return ET.fromstring(raw)


def _worksheet_parts(archive, plan):
    workbook = _read_xml(archive, "xl/workbook.xml")
    relationships = _read_xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {item.get("Id"): item.get("Target") for item in relationships}
    sheets = list(workbook.findall(f"{{{_S}}}sheets/{{{_S}}}sheet"))
    if [item.get("name") for item in sheets] != [sheet.name for sheet in plan.sheets]: _fail("SPREADSHEET_READBACK_FAILED")
    result = []
    for item in sheets:
        target = targets[item.get(f"{{{_R}}}id")]
        if target.startswith("/xl/worksheets/"): path = target.lstrip("/")
        elif re.fullmatch(r"worksheets/sheet[0-9]+\.xml", target): path = "xl/" + target
        else: _fail("SPREADSHEET_PACKAGE_INVALID")
        result.append(path)
    return workbook, result


def _formats(archive):
    styles = _read_xml(archive, "xl/styles.xml")
    formats = dict(_BUILTIN_FORMATS)
    if len(styles.findall(f"{{{_S}}}numFmts")) > 1 or len(styles.findall(f"{{{_S}}}cellXfs")) != 1:
        _fail("SPREADSHEET_FORMAT_READBACK_FAILED")
    custom = styles.find(f"{{{_S}}}numFmts")
    if custom is not None:
        if custom.get("count") is not None and custom.get("count") != str(len(custom)): _fail("SPREADSHEET_FORMAT_READBACK_FAILED")
        for item in custom:
            identity = item.get("numFmtId", "")
            if (item.tag != f"{{{_S}}}numFmt" or not identity.isascii() or not identity.isdigit() or len(identity) > 5 or
                not 164 <= int(identity) <= 65535 or int(identity) in formats or item.get("formatCode") not in NUMBER_FORMATS):
                _fail("SPREADSHEET_FORMAT_READBACK_FAILED")
            formats[int(identity)] = item.get("formatCode")
    xfs = styles.find(f"{{{_S}}}cellXfs")
    if not 1 <= len(xfs) <= 128 or xfs.get("count") is not None and xfs.get("count") != str(len(xfs)):
        _fail("SPREADSHEET_FORMAT_READBACK_FAILED")
    result = []
    for item in xfs:
        identity = item.get("numFmtId", "0")
        if item.tag != f"{{{_S}}}xf" or not identity.isascii() or not identity.isdigit() or len(identity) > 5 or int(identity) not in formats:
            _fail("SPREADSHEET_FORMAT_READBACK_FAILED")
        result.append(formats[int(identity)])
    return result


def verify_xlsx(raw, plan):
    """Reopen ZIP/XML independently of openpyxl; do not run formulas or Excel."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any("vba" in name.casefold() or "externallinks" in name.casefold() for name in names):
                _fail("SPREADSHEET_PACKAGE_INVALID")
            for name in names:
                if name.endswith(".rels") and any(item.get("TargetMode") == "External" for item in _read_xml(archive, name)):
                    _fail("SPREADSHEET_PACKAGE_INVALID")
            workbook, parts = _worksheet_parts(archive, plan)
            calc = workbook.find(f"{{{_S}}}calcPr")
            if calc is None or calc.get("calcMode") != "auto" or calc.get("fullCalcOnLoad") != "1" or calc.get("forceFullCalc") != "1":
                _fail("SPREADSHEET_READBACK_FAILED")
            formats = _formats(archive)
            for sheet, path in zip(plan.sheets, parts):
                root = _read_xml(archive, path)
                cells = {}
                for value in root.findall(f"{{{_S}}}sheetData/{{{_S}}}row/{{{_S}}}c"):
                    address = value.get("r")
                    if address in cells or address not in sheet.cells: _fail("SPREADSHEET_READBACK_FAILED")
                    cells[address] = value
                for address, expected in sheet.cells.items():
                    cell = cells.get(address)
                    if cell is None:
                        if expected.kind != "blank": _fail("SPREADSHEET_READBACK_FAILED")
                        continue
                    counts = {name: len(cell.findall(f"{{{_S}}}{name}")) for name in ("f", "v", "is")}
                    if (any(count > 1 for count in counts.values()) or
                        any(item.tag not in {f"{{{_S}}}f", f"{{{_S}}}v", f"{{{_S}}}is"} for item in cell) or
                        counts["is"] and (counts["f"] or counts["v"])):
                        _fail("SPREADSHEET_READBACK_FAILED")
                    formula = cell.find(f"{{{_S}}}f")
                    value = cell.findtext(f"{{{_S}}}v")
                    if expected.kind == "formula":
                        if formula is None or len(formula) or formula.text != expected.formula or formula.attrib or cell.get("t", "n") != "n" or value != decimal_text(expected.cache):
                            _fail("SPREADSHEET_READBACK_FAILED")
                    elif formula is not None: _fail("SPREADSHEET_READBACK_FAILED")
                    elif expected.kind == "string":
                        text = "".join(item.text or "" for item in cell.findall(f".//{{{_S}}}t"))
                        if cell.get("t") != "inlineStr" or text != expected.value: _fail("SPREADSHEET_READBACK_FAILED")
                    elif expected.kind == "boolean":
                        if cell.get("t") != "b" or value != ("1" if expected.value else "0"): _fail("SPREADSHEET_READBACK_FAILED")
                    elif expected.kind in ("typed_number", "number"):
                        number = expected.value if expected.kind == "typed_number" else expected.cache
                        if cell.get("t", "n") != "n" or Decimal(value) != number: _fail("SPREADSHEET_READBACK_FAILED")
                    elif value is not None: _fail("SPREADSHEET_READBACK_FAILED")
                    style = cell.get("s", "0")
                    if (not style.isascii() or not style.isdigit() or len(style) > 5 or int(style) >= len(formats) or
                        formats[int(style)] != expected.number_format): _fail("SPREADSHEET_FORMAT_READBACK_FAILED")
    except SpreadsheetError: raise
    except Exception: _fail("SPREADSHEET_READBACK_FAILED")
    return plan.verification()


def cache_and_verify_xlsx(raw, plan):
    try:
        updates = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            workbook, parts = _worksheet_parts(archive, plan)
            calc = workbook.find(f"{{{_S}}}calcPr")
            if calc is None: calc = ET.SubElement(workbook, f"{{{_S}}}calcPr")
            calc.set("calcMode", "auto"); calc.set("fullCalcOnLoad", "1"); calc.set("forceFullCalc", "1")
            updates["xl/workbook.xml"] = ET.tostring(workbook, encoding="utf-8", xml_declaration=True)
            for sheet, path in zip(plan.sheets, parts):
                root = _read_xml(archive, path)
                for cell in root.findall(f"{{{_S}}}sheetData/{{{_S}}}row/{{{_S}}}c"):
                    expected = sheet.cells.get(cell.get("r"))
                    if expected is None: _fail("SPREADSHEET_READBACK_FAILED")
                    if expected.kind in ("formula", "typed_number"):
                        if expected.kind == "formula" and cell.findtext(f"{{{_S}}}f") != expected.formula: _fail("SPREADSHEET_READBACK_FAILED")
                        cell.set("t", "n")
                        value = cell.find(f"{{{_S}}}v")
                        if value is None: value = ET.SubElement(cell, f"{{{_S}}}v")
                        value.text = decimal_text(expected.cache if expected.kind == "formula" else expected.value)
                updates[path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as written:
                for info in archive.infolist(): written.writestr(info, updates.get(info.filename, archive.read(info.filename)))
        result = output.getvalue()
        verify_xlsx(result, plan)
        return result
    except SpreadsheetError: raise
    except Exception: _fail("SPREADSHEET_READBACK_FAILED")
