"""Bounded, deterministic arithmetic and read-only CSV aggregation.

All numeric results are decimal strings, never JSON binary floating point.
``calculate`` is an arithmetic DSL, not a Python execution facility: only
listed syntax/functions are visited and no names, attributes or code run.
``analyze_csv`` reads at most the existing workspace file budget and rejects
ambiguous/malformed rows instead of silently producing a partial total.
"""
from __future__ import annotations

import ast
import csv
from decimal import (
    Decimal, DecimalException, DivisionByZero, Inexact, InvalidOperation,
    Overflow, ROUND_HALF_EVEN, Rounded, localcontext,
)
import hashlib
import io
import json
from pathlib import Path
import re
import time

from safe_files import MAX_FILE_BYTES, WorkspaceFiles


MAX_EXPRESSION_CHARACTERS = 2000
MAX_AST_NODES = 256
MAX_AST_DEPTH = 24
MAX_FUNCTION_VALUES = 100
MAX_NUMBER_DIGITS = 60
MAX_NUMBER_EXPONENT = 100
MAX_RESULT_ADJUSTED = 200
CALCULATION_PRECISION = 80
MAX_CSV_ROWS = 10_000
MAX_CSV_COLUMNS = 100
MAX_CSV_GROUPS = 128
MAX_CSV_GROUP_CHARACTERS = 128
MAX_CSV_CELL_CHARACTERS = 4096
MAX_SECONDS = 1.0

_LITERAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_AMOUNT = re.compile(r"[+-]?(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_CURRENCIES = {"¥": "CNY", "￥": "CNY", "$": "USD", "€": "EUR", "£": "GBP"}
_MESSAGES = {
    "INVALID_EXPRESSION": "计算表达式无效，仅支持有限的十进制算术。",
    "UNSUPPORTED_EXPRESSION": "表达式包含不支持的语法、名称或函数。",
    "CALCULATION_LIMIT": "计算超过允许的长度、复杂度或时间范围。",
    "NUMBER_RANGE": "数值超出允许的位数或指数范围。",
    "DIVISION_BY_ZERO": "计算不能除以零。",
    "INVALID_ARITHMETIC": "此算术操作无效。",
    "INVALID_CSV_OPTIONS": "CSV 列名或分隔符无效。",
    "FILE_UNAVAILABLE": "无法读取工作区内的 CSV 文件，或文件超过大小限制。",
    "OUTSIDE_WORKSPACE": "只能读取授权工作区内的 CSV 文件。",
    "CSV_ENCODING": "CSV 必须使用 UTF-8 编码。",
    "CSV_FORMAT": "CSV 格式无效，列数、引号或字段长度不正确。",
    "CSV_HEADER": "CSV 缺少有效表头，或表头重复。",
    "CSV_COLUMN": "CSV 中没有指定的数值列或分组列。",
    "CSV_NUMBER": "CSV 中包含空缺、非数字或格式不明确的金额。",
    "CSV_LIMIT": "CSV 超过允许的行数、分组数或处理时间。",
    "MIXED_CURRENCIES": "CSV 含有不同币种，不能直接合计；请先明确换算。",
    "SOURCE_CHANGED": "读取期间 CSV 文件发生变化，请重新查询。",
}


class DataToolError(ValueError):
    """Static errors carry a code/line without reflecting file data or paths."""

    def __init__(self, code: str, *, line: int | None = None):
        self.code = code
        self.line = line
        message = _MESSAGES.get(code, _MESSAGES["INVALID_ARITHMETIC"])
        super().__init__(message + (f"（第 {line} 行）" if line is not None else ""))


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _validate_number(value: Decimal, *, result: bool = False) -> Decimal:
    if not value.is_finite():
        raise DataToolError("NUMBER_RANGE")
    parts = value.as_tuple()
    bound = MAX_RESULT_ADJUSTED if result else MAX_NUMBER_EXPONENT
    if (abs(value.adjusted()) > bound or abs(parts.exponent) > (200 if result else MAX_NUMBER_EXPONENT)
            or (not result and len(parts.digits) > MAX_NUMBER_DIGITS)):
        raise DataToolError("NUMBER_RANGE")
    return value


def _amount(value: str) -> tuple[Decimal, str | None]:
    """Parse explicit thousands/currency conventions; reject ambiguous commas."""
    text = value.strip()
    if not text or len(text) > 160:
        raise DataToolError("CSV_NUMBER")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
        if text.startswith(("+", "-")):
            raise DataToolError("CSV_NUMBER")
    currency = None
    prefix_sign = ""
    if text.startswith(("+", "-")):
        prefix_sign, text = text[0], text[1:]
    if text[:1] in _CURRENCIES:
        currency, text = _CURRENCIES[text[0]], text[1:].strip()
    if currency is None:
        for code in ("CNY", "USD", "EUR", "GBP"):
            if text.upper().startswith(code):
                currency, text = code, text[len(code):].strip()
                break
    for suffix, name in (("CNY", "CNY"), ("USD", "USD"), ("EUR", "EUR"), ("GBP", "GBP"), ("元", "CNY")):
        if text.upper().endswith(suffix):
            if currency is not None and currency != name:
                raise DataToolError("MIXED_CURRENCIES")
            currency = name
            text = text[:-len(suffix)].strip()
            break
    if prefix_sign:
        if text.startswith(("+", "-")):
            raise DataToolError("CSV_NUMBER")
        text = prefix_sign + text
    if negative and text.startswith(("+", "-")):
        raise DataToolError("CSV_NUMBER")
    if not _AMOUNT.fullmatch(text):
        raise DataToolError("CSV_NUMBER")
    try:
        number = _validate_number(Decimal(text.replace(",", "")))
    except (DecimalException, DataToolError):
        raise DataToolError("CSV_NUMBER") from None
    return (-number if negative else number), currency


class _Arithmetic:
    def __init__(self, text: str):
        self.text = text
        self.deadline = time.monotonic() + MAX_SECONDS

    def _check(self, depth):
        if depth > MAX_AST_DEPTH or time.monotonic() > self.deadline:
            raise DataToolError("CALCULATION_LIMIT")

    def visit(self, node: ast.AST, depth=0) -> Decimal:
        self._check(depth)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            literal = ast.get_source_segment(self.text, node)
            if not literal or len(literal) > 160 or not _LITERAL.fullmatch(literal):
                raise DataToolError("UNSUPPORTED_EXPRESSION")
            return _validate_number(Decimal(literal))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self.visit(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = self.visit(node.left, depth + 1), self.visit(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, (ast.FloorDiv, ast.Mod)):
                if right.is_zero():
                    raise DataToolError("DIVISION_BY_ZERO")
                # Decimal // truncates towards zero; this DSL uses mathematical
                # floor, matching the familiar Python meaning even for negatives.
                ln, ld = left.as_integer_ratio()
                rn, rd = right.as_integer_ratio()
                quotient = Decimal((ln * rd) // (ld * rn))
                if isinstance(node.op, ast.FloorDiv):
                    value = quotient
                else:
                    with localcontext() as exact:
                        exact.prec = 500
                        value = left - quotient * right
            elif isinstance(node.op, ast.Pow):
                if right != right.to_integral_value() or abs(right) > 100:
                    raise DataToolError("CALCULATION_LIMIT")
                value = left ** int(right)
            else:
                raise DataToolError("UNSUPPORTED_EXPRESSION")
            return _validate_number(value, result=True)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name not in {"abs", "min", "max", "sum", "round"}:
                raise DataToolError("UNSUPPORTED_EXPRESSION")
            args = node.args
            if name in {"min", "max", "sum"} and len(args) == 1 and isinstance(args[0], (ast.List, ast.Tuple)):
                args = args[0].elts
            if len(args) > MAX_FUNCTION_VALUES:
                raise DataToolError("CALCULATION_LIMIT")
            values = [self.visit(arg, depth + 1) for arg in args]
            if name == "abs" and len(values) == 1:
                value = abs(values[0])
            elif name == "sum":
                value = sum(values, Decimal(0))
            elif name in {"min", "max"} and values:
                value = min(values) if name == "min" else max(values)
            elif name == "round" and 1 <= len(values) <= 2:
                places = values[1] if len(values) == 2 else Decimal(0)
                if places != places.to_integral_value() or abs(places) > 100:
                    raise DataToolError("CALCULATION_LIMIT")
                value = values[0].quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_EVEN)
            else:
                raise DataToolError("UNSUPPORTED_EXPRESSION")
            return _validate_number(value, result=True)
        raise DataToolError("UNSUPPORTED_EXPRESSION")


class DataTools:
    def __init__(self, workspace: Path):
        self.files = WorkspaceFiles(Path(workspace))

    def calculate(self, expression: str) -> str:
        """+, -, *, /, //, %, small integer **; abs/min/max/sum/round only.

        Round uses HALF_EVEN; repeating or rounded arithmetic sets exact=false.
        Source decimal literals retain their spelling rather than becoming floats.
        """
        if not isinstance(expression, str) or not expression.strip():
            raise DataToolError("INVALID_EXPRESSION")
        text = expression.strip()
        if len(text) > MAX_EXPRESSION_CHARACTERS:
            raise DataToolError("CALCULATION_LIMIT")
        try:
            tree = ast.parse(text, mode="eval")
            if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
                raise DataToolError("CALCULATION_LIMIT")
            with localcontext() as context:
                context.prec = CALCULATION_PRECISION
                context.Emax, context.Emin = 1000, -1000
                context.clear_flags()
                value = _Arithmetic(text).visit(tree.body)
                exact = not (context.flags[Inexact] or context.flags[Rounded])
            return _json({"tool": "calculate", "read_only": True, "result": _decimal_text(value),
                          "exact": exact, "precision": CALCULATION_PRECISION, "rounding": "ROUND_HALF_EVEN"})
        except DataToolError:
            raise
        except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError):
            raise DataToolError("INVALID_EXPRESSION") from None
        except DivisionByZero:
            raise DataToolError("DIVISION_BY_ZERO") from None
        except (Overflow, InvalidOperation, DecimalException):
            raise DataToolError("INVALID_ARITHMETIC") from None

    def analyze_csv(self, path: str, value_column: str, group_by: str = "", delimiter: str = ",") -> str:
        """Return exact totals with source identity; never alter the CSV.

        Accept decimal numbers, well-formed comma thousands, explicit currency
        symbols/codes and accounting parentheses. Different marked currencies
        cannot be added. Blank records are counted explicitly; missing values
        in a nonblank record are errors. No malformed row is silently dropped.
        """
        if (not isinstance(value_column, str) or not value_column.strip() or len(value_column) > 256
                or not isinstance(group_by, str) or len(group_by) > 256
                or not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in {'\r', '\n', '\0', '"'}
                or any(ord(char) < 32 and char != '\t' for char in delimiter)):
            raise DataToolError("INVALID_CSV_OPTIONS")
        value_column, group_by = value_column.strip(), group_by.strip()
        started = time.monotonic()
        try:
            target = self.files.resolve(path)
        except (ValueError, OSError, RuntimeError):
            raise DataToolError("OUTSIDE_WORKSPACE") from None
        try:
            before = target.stat()
            if not target.is_file() or before.st_size > MAX_FILE_BYTES:
                raise DataToolError("FILE_UNAVAILABLE")
            with target.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            after = target.stat()
            if (len(raw) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ino)
                    != (after.st_size, after.st_mtime_ns, after.st_ino)
                    or self.files.resolve(str(target)) != target):
                raise DataToolError("SOURCE_CHANGED")
        except DataToolError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise DataToolError("FILE_UNAVAILABLE") from None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            raise DataToolError("CSV_ENCODING") from None
        if "\0" in text:
            raise DataToolError("CSV_FORMAT")
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        try:
            header = next(reader)
        except (StopIteration, csv.Error):
            raise DataToolError("CSV_HEADER") from None
        header = [item.strip() for item in header]
        if (not header or len(header) > MAX_CSV_COLUMNS
                or any(not item or len(item) > MAX_CSV_CELL_CHARACTERS for item in header)
                or len(set(header)) != len(header)):
            raise DataToolError("CSV_HEADER")
        if value_column not in header or group_by and group_by not in header:
            raise DataToolError("CSV_COLUMN")
        amount_index = header.index(value_column)
        group_index = header.index(group_by) if group_by else None
        total, groups, currencies = Decimal(0), {}, set()
        count, blanks, unmarked = 0, 0, 0
        try:
            with localcontext() as context:
                # Literal bounds span at most ~260 decimal places and the row
                # cap adds at most five digits. Trap every unexpected rounding.
                context.prec = 400
                context.traps[Inexact] = context.traps[Rounded] = True
                for cells in reader:
                    line = reader.line_num
                    if time.monotonic() - started > MAX_SECONDS or count + blanks >= MAX_CSV_ROWS:
                        raise DataToolError("CSV_LIMIT", line=line)
                    if not cells or all(not cell.strip() for cell in cells):
                        blanks += 1
                        continue
                    if len(cells) != len(header) or any(len(cell) > MAX_CSV_CELL_CHARACTERS for cell in cells):
                        raise DataToolError("CSV_FORMAT", line=line)
                    try:
                        value, currency = _amount(cells[amount_index])
                    except DataToolError as exc:
                        raise DataToolError(exc.code, line=line) from None
                    if currency:
                        currencies.add(currency)
                        if len(currencies) > 1:
                            raise DataToolError("MIXED_CURRENCIES", line=line)
                    else:
                        unmarked += 1
                    if group_index is not None:
                        group = cells[group_index].strip()
                        if len(group) > MAX_CSV_GROUP_CHARACTERS or (group not in groups and len(groups) >= MAX_CSV_GROUPS):
                            raise DataToolError("CSV_LIMIT", line=line)
                        groups[group] = groups.get(group, Decimal(0)) + value
                    total += value
                    count += 1
        except DataToolError:
            raise
        except csv.Error:
            raise DataToolError("CSV_FORMAT", line=reader.line_num) from None
        except DecimalException:
            raise DataToolError("CSV_NUMBER", line=reader.line_num) from None
        warnings = ["UNMARKED_CURRENCY_VALUES"] if currencies and unmarked else []
        currency = next(iter(currencies)) if currencies and not unmarked else None
        return _json({"tool": "analyze_csv", "read_only": True, "exact": True,
                      "source_path": str(target.relative_to(self.files.root)),
                      "source_sha256": hashlib.sha256(raw).hexdigest(), "source_bytes": len(raw),
                      "value_column": value_column, "group_by": group_by,
                      "record_count": count, "blank_records_skipped": blanks, "empty": count == 0,
                      "total": _decimal_text(total),
                      "category_totals": {name: _decimal_text(value) for name, value in sorted(groups.items())},
                      "currency": currency, "currency_labels": sorted(currencies),
                      "unmarked_currency_rows": unmarked, "warnings": warnings})
