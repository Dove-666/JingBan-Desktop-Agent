"""One independent CSV-to-editable-XLSX benchmark; stdlib only, no Excel.

This is a fixed three-item budget oracle, not an Excel evaluator. Reference
values come directly from immutable CSV using Decimal multiplication/ROUND.
No production document/formula module, provider, executable or model is used.
"""
from __future__ import annotations

from copy import deepcopy
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile


KIND = "xlsx_editable_formulas"
CASE = "three_item_budget_round_v1"
SOURCE = "source.csv"
OUTPUT = "budget.xlsx"
SHEET = "预算"
SOURCE_TEXT = "item,quantity,unit_price\n鲸鱼贴纸,3,19.995\n笔记本,2,7.235\n包装袋,5,0.105\n"
LITERAL = "=SUM(D2:D4)"
SCOPE = "source_csv_decimal_ooxml_formulas_cache_formats_not_excel_recalculation"
MAX_SOURCE = 8192
MAX_ARTIFACT = 2 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024
MAX_XML = 2 * 1024 * 1024
MAX_MEMBERS = 128
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
T = "http://schemas.openxmlformats.org/package/2006/content-types"
_FORMULAS = {"D2": "ROUND(B2*C2,2)", "D3": "ROUND(B3*C3,2)", "D4": "ROUND(B4*C4,2)", "D5": "SUM(D2:D4)"}
_SPEC = {"kind": KIND, "case": CASE, "path": OUTPUT, "source_path": SOURCE}


class XlsxOracleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise XlsxOracleError(code) from None


def catalog_entry():
    return deepcopy({"id": "xlsx_editable_formula_budget", "suite": "real_model", "category": "spreadsheets_formula_subset",
        "implementation": "implemented",
        "objective": "Create one editable three-item budget with source-derived ROUND/SUM formulas, numeric caches, exact formats and a formula-like literal; no general Excel editing or real recalculation claim.",
        "prompt": "请读取 source.csv，创建 budget.xlsx，工作表名为“预算”。第1行从A到D依次为“商品、数量、单价、金额”；第2至4行按CSV顺序填写商品，数量和单价必须是数值，保留原单价的3位小数。B2:B4用整数格式0，C2:C4用格式0.000。D2、D3、D4分别保留可编辑的ROUND(B2*C2,2)、ROUND(B3*C3,2)、ROUND(B4*C4,2)公式，使用原单价计算，不先改动单价；D2:D4用格式0.00。A5写“合计”，D5保留可编辑的SUM(D2:D4)公式并用格式0.00。A6写“字面示例”，B6原样保存 =SUM(D2:D4) 这段普通文本，不能成为公式。保存后检查实际公式、计算缓存和格式，保持 source.csv 字节完全不变。无需PDF、截图或启动Excel。",
        "fixtures": [{"path": SOURCE, "text": SOURCE_TEXT}], "oracles": [deepcopy(_SPEC)]})


def validate(value):
    if type(value) is not dict or set(value) != set(_SPEC) or any(type(value[key]) is not str or value[key] != expected for key, expected in _SPEC.items()):
        _fail("XLSX_ORACLE_INVALID_SPEC")
    return True


def _case(case):
    if type(case) is not str or case != CASE: _fail("XLSX_ORACLE_UNKNOWN_CASE")


def _alias(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _root(workspace):
    try:
        root = Path(workspace).absolute()
        for path in (root, *root.parents):
            if _alias(path.lstat()): _fail("XLSX_ORACLE_WORKSPACE_ESCAPE")
        if not root.is_dir() or root.resolve(strict=True) != root: _fail("XLSX_ORACLE_INVALID_WORKSPACE")
        return root
    except OSError: _fail("XLSX_ORACLE_FILE_UNAVAILABLE")


def _read(workspace, name, maximum):
    if name not in (SOURCE, OUTPUT): _fail("XLSX_ORACLE_INVALID_SPEC")
    root = _root(workspace)
    path = root / name
    try:
        before = path.lstat()
        if _alias(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or path.resolve(strict=True).parent != root:
            _fail("XLSX_ORACLE_WORKSPACE_ESCAPE")
        if before.st_size > maximum: _fail("XLSX_ORACLE_FILE_LIMIT")
        with path.open("rb") as source:
            raw = source.read(maximum + 1)
        after = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(raw) > maximum: _fail("XLSX_ORACLE_FILE_LIMIT")
        if _alias(after) or identity(before) != identity(after) or path.resolve(strict=True).parent != root:
            _fail("XLSX_ORACLE_FILE_CHANGED")
        return raw
    except OSError: _fail("XLSX_ORACLE_FILE_UNAVAILABLE")


def _numeric(text):
    if type(text) is not str or len(text) > 128 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", text):
        _fail("XLSX_ORACLE_INVALID_NUMBER")
    try:
        value = Decimal(text)
        if not value.is_finite() or abs(value.adjusted()) > 100 or abs(value.as_tuple().exponent) > 100:
            _fail("XLSX_ORACLE_INVALID_NUMBER")
        return value
    except InvalidOperation: _fail("XLSX_ORACLE_INVALID_NUMBER")


def _reference(raw):
    if raw != SOURCE_TEXT.encode("utf-8"): _fail("XLSX_ORACLE_SOURCE_CHANGED")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        if reader.fieldnames != ["item", "quantity", "unit_price"]: _fail("XLSX_ORACLE_INVALID_SOURCE")
        rows = list(reader)
        if len(rows) != 3 or any(set(row) != {"item", "quantity", "unit_price"} for row in rows): _fail("XLSX_ORACLE_INVALID_SOURCE")
        values, total = [], Decimal(0)
        with localcontext() as context:
            context.prec = 34
            for row in rows:
                quantity, price = _numeric(row["quantity"]), _numeric(row["unit_price"])
                amount = (quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                total += amount
                values.append((row["item"], quantity, price, amount))
        return values, total
    except (UnicodeError, csv.Error, TypeError): _fail("XLSX_ORACLE_INVALID_SOURCE")


def _archive(raw):
    archive = None
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = archive.infolist(); names = [item.filename for item in entries]
        if (not 1 <= len(entries) <= MAX_MEMBERS or len(names) != len(set(names)) or
            sum(item.file_size for item in entries) > MAX_EXPANDED or any(
                item.file_size > MAX_XML or item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                stat.S_ISLNK(item.external_attr >> 16) or not item.filename or item.filename.startswith(("/", "\\")) or
                "\\" in item.filename or ".." in PurePosixPath(item.filename).parts or
                any(word in item.filename.casefold() for word in ("vbaproject", "externallinks", "embeddings"))
                for item in entries)):
            _fail("XLSX_ORACLE_UNSAFE_PACKAGE")
        return archive
    except XlsxOracleError:
        if archive: archive.close()
        raise
    except (zipfile.BadZipFile, ValueError):
        if archive: archive.close()
        _fail("XLSX_ORACLE_INVALID_PACKAGE")


def _xml(archive, name, namespace, local):
    try:
        raw = archive.read(name)
        if len(raw) > MAX_XML or b"\0" in raw: _fail("XLSX_ORACLE_UNSAFE_XML")
        text = raw.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper(): _fail("XLSX_ORACLE_UNSAFE_XML")
        root = ET.fromstring(text)
        if root.tag != f"{{{namespace}}}{local}": _fail("XLSX_ORACLE_INVALID_NAMESPACE")
        return root
    except XlsxOracleError: raise
    except (KeyError, ET.ParseError, UnicodeError, OSError, zipfile.BadZipFile): _fail("XLSX_ORACLE_INVALID_XML")


def _rels(archive, path):
    root = _xml(archive, path, P, "Relationships")
    result = {}
    for value in root:
        if value.tag != f"{{{P}}}Relationship" or set(value.attrib) - {"Id", "Target", "Type", "TargetMode"}:
            _fail("XLSX_ORACLE_INVALID_RELATIONSHIP")
        identity, target, kind = value.get("Id"), value.get("Target"), value.get("Type")
        if (not identity or identity in result or not target or not kind or value.get("TargetMode") not in (None, "Internal") or
            kind.endswith("/hyperlink") or ":" in target):
            _fail("XLSX_ORACLE_EXTERNAL_RELATIONSHIP")
        result[identity] = (kind, target)
    return result


def _part(base, target):
    if (type(target) is not str or "\\" in target or target.startswith("//") or ".." in PurePosixPath(target).parts or ":" in target):
        _fail("XLSX_ORACLE_INVALID_RELATIONSHIP")
    return target.lstrip("/") if target.startswith("/") else str(PurePosixPath(base) / target)


def _find_target(relationships, suffix, required=True):
    values = [target for kind, target in relationships.values() if kind == R + "/" + suffix]
    if len(values) != 1:
        if not values and not required: return None
        _fail("XLSX_ORACLE_INVALID_RELATIONSHIP")
    return values[0]


def _format_codes(archive, path):
    root = _xml(archive, path, S, "styleSheet")
    formats = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%"}
    seen = set()
    for item in root.findall(f"{{{S}}}numFmts/{{{S}}}numFmt"):
        identity = item.get("numFmtId", "")
        if not identity.isascii() or not identity.isdigit() or len(identity) > 5: _fail("XLSX_ORACLE_INVALID_STYLES")
        identity = int(identity)
        if identity < 164 or identity in seen or len(item.get("formatCode", "")) > 128: _fail("XLSX_ORACLE_INVALID_STYLES")
        seen.add(identity); formats[identity] = item.get("formatCode")
    result = []
    for item in root.findall(f"{{{S}}}cellXfs/{{{S}}}xf"):
        identity = item.get("numFmtId", "0")
        if not identity.isascii() or not identity.isdigit() or len(identity) > 5: _fail("XLSX_ORACLE_INVALID_STYLES")
        result.append(formats.get(int(identity)))
    if not 1 <= len(result) <= 128: _fail("XLSX_ORACLE_INVALID_STYLES")
    return result


def _text(cell, shared):
    kind = cell.get("t")
    if kind == "inlineStr":
        if len(cell.findall(f"{{{S}}}is")) != 1: _fail("XLSX_ORACLE_LITERAL_MISMATCH")
        return "".join(item.text or "" for item in cell.findall(f"{{{S}}}is/.//{{{S}}}t"))
    value = cell.findtext(f"{{{S}}}v", "")
    if kind == "str": return value
    if kind == "s" and value.isdigit() and len(value) <= 5 and int(value) < len(shared): return shared[int(value)]
    _fail("XLSX_ORACLE_LITERAL_MISMATCH")


def _check_xlsx(raw, rows, total):
    with _archive(raw) as archive:
        _xml(archive, "[Content_Types].xml", T, "Types")
        for path in archive.namelist():
            if path.endswith(".rels"): _rels(archive, path)
        workbook_path = _part("", _find_target(_rels(archive, "_rels/.rels"), "officeDocument"))
        workbook = _xml(archive, workbook_path, S, "workbook")
        sheets = list(workbook.findall(f"{{{S}}}sheets/{{{S}}}sheet"))
        if len(sheets) != 1 or sheets[0].get("name") != SHEET or sheets[0].get("state", "visible") != "visible":
            _fail("XLSX_ORACLE_SHEET_MISMATCH")
        parent = str(PurePosixPath(workbook_path).parent)
        relationships = _rels(archive, str(PurePosixPath(parent) / "_rels" / (PurePosixPath(workbook_path).name + ".rels")))
        sheet_id = sheets[0].get(f"{{{R}}}id")
        if sheet_id not in relationships or relationships[sheet_id][0] != R + "/worksheet": _fail("XLSX_ORACLE_INVALID_RELATIONSHIP")
        sheet_path = _part(parent, relationships[sheet_id][1])
        formats = _format_codes(archive, _part(parent, _find_target(relationships, "styles")))
        shared = []
        shared_path = _find_target(relationships, "sharedStrings", required=False)
        if shared_path:
            strings = _xml(archive, _part(parent, shared_path), S, "sst")
            for item in strings.findall(f"{{{S}}}si"):
                shared.append("".join(node.text or "" for node in item.findall(f".//{{{S}}}t")))
            if len(shared) > 1024: _fail("XLSX_ORACLE_FILE_LIMIT")
        sheet = _xml(archive, sheet_path, S, "worksheet")
        cells = {}
        for row in sheet.findall(f"{{{S}}}sheetData/{{{S}}}row"):
            if row.get("hidden") in ("1", "true"): _fail("XLSX_ORACLE_SHEET_MISMATCH")
            for cell in row.findall(f"{{{S}}}c"):
                address = cell.get("r", "")
                if address in cells or not re.fullmatch(r"[A-D][1-6]", address): _fail("XLSX_ORACLE_UNEXPECTED_CELL")
                cells[address] = cell
                if len(cells) > 24 or len(cell.findall(f"{{{S}}}v")) > 1: _fail("XLSX_ORACLE_INVALID_CELL")
        wanted_text = {"A1": "商品", "B1": "数量", "C1": "单价", "D1": "金额", "A5": "合计", "A6": "字面示例", "B6": LITERAL}
        wanted_numbers, wanted_formats = {}, {}
        for row_number, (item, quantity, price, amount) in enumerate(rows, 2):
            wanted_text[f"A{row_number}"] = item
            wanted_numbers.update({f"B{row_number}": quantity, f"C{row_number}": price, f"D{row_number}": amount})
            wanted_formats.update({f"B{row_number}": "0", f"C{row_number}": "0.000", f"D{row_number}": "0.00"})
        wanted_numbers["D5"], wanted_formats["D5"] = total, "0.00"
        required = set(wanted_text) | set(wanted_numbers)
        if not required <= set(cells): _fail("XLSX_ORACLE_MISSING_CELL")
        for address, cell in cells.items():
            actual_formulas = cell.findall(f"{{{S}}}f")
            if address in _FORMULAS:
                if len(actual_formulas) != 1: _fail("XLSX_ORACLE_FORMULA_MISSING")
                text = actual_formulas[0].text or ""
                normalized = "".join(text.split()).replace("$", "").upper()
                if actual_formulas[0].attrib or len(text) > 256 or normalized != _FORMULAS[address]: _fail("XLSX_ORACLE_FORMULA_MISMATCH")
            elif actual_formulas: _fail("XLSX_ORACLE_UNEXPECTED_FORMULA")
            if address in wanted_text:
                if _text(cell, shared) != wanted_text[address]: _fail("XLSX_ORACLE_LITERAL_MISMATCH")
            elif address in wanted_numbers:
                if cell.get("t", "n") != "n" or _numeric(cell.findtext(f"{{{S}}}v")) != wanted_numbers[address]:
                    _fail("XLSX_ORACLE_NUMERIC_CACHE_MISMATCH")
            elif cell.find(f"{{{S}}}v") is not None or cell.find(f"{{{S}}}is") is not None:
                _fail("XLSX_ORACLE_UNEXPECTED_CELL")
            if address in wanted_formats:
                index = cell.get("s", "0")
                if not index.isascii() or not index.isdigit() or len(index) > 4 or int(index) >= len(formats) or formats[int(index)] != wanted_formats[address]:
                    _fail("XLSX_ORACLE_FORMAT_MISMATCH")
        if len(sheet.findall(f".//{{{S}}}f")) != len(_FORMULAS): _fail("XLSX_ORACLE_UNEXPECTED_FORMULA")


def evaluate(workspace, case):
    report = {"passed": False, "issues": [], "scope": SCOPE}
    try:
        _case(case)
        source = _read(workspace, SOURCE, MAX_SOURCE)
        rows, total = _reference(source)
        artifact = _read(workspace, OUTPUT, MAX_ARTIFACT)
        _check_xlsx(artifact, rows, total)
        if _read(workspace, SOURCE, MAX_SOURCE) != source or _read(workspace, OUTPUT, MAX_ARTIFACT) != artifact:
            _fail("XLSX_ORACLE_FILE_CHANGED")
        report.update(passed=True, source_sha256=hashlib.sha256(source).hexdigest(), artifact_sha256=hashlib.sha256(artifact).hexdigest())
    except XlsxOracleError as error: report["issues"] = [error.code]
    except Exception: report["issues"] = ["XLSX_ORACLE_INVALID_OUTPUT"]
    return report


def _element(tag, **attributes): return ET.Element(tag, {key: str(value) for key, value in attributes.items()})
def _bytes(root): return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _reference_package(rows, total):
    types = _element(f"{{{T}}}Types")
    ET.SubElement(types, f"{{{T}}}Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, f"{{{T}}}Default", Extension="xml", ContentType="application/xml")
    for part, content in (("/xl/workbook.xml", "sheet.main"), ("/xl/worksheets/sheet1.xml", "worksheet"), ("/xl/styles.xml", "styles")):
        ET.SubElement(types, f"{{{T}}}Override", PartName=part, ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml." + content + "+xml")
    root_rels = _element(f"{{{P}}}Relationships")
    ET.SubElement(root_rels, f"{{{P}}}Relationship", Id="rId1", Type=R + "/officeDocument", Target="xl/workbook.xml")
    workbook = _element(f"{{{S}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{S}}}sheets")
    ET.SubElement(sheets, f"{{{S}}}sheet", {"name": SHEET, "sheetId": "1", f"{{{R}}}id": "rId1"})
    ET.SubElement(workbook, f"{{{S}}}calcPr", calcMode="auto", fullCalcOnLoad="1", forceFullCalc="1")
    relationships = _element(f"{{{P}}}Relationships")
    for identity, suffix, target in (("rId1", "worksheet", "worksheets/sheet1.xml"), ("rId2", "styles", "styles.xml")):
        ET.SubElement(relationships, f"{{{P}}}Relationship", Id=identity, Type=R + "/" + suffix, Target=target)
    styles = _element(f"{{{S}}}styleSheet")
    custom = ET.SubElement(styles, f"{{{S}}}numFmts", count="1")
    ET.SubElement(custom, f"{{{S}}}numFmt", numFmtId="164", formatCode="0.000")
    fonts = ET.SubElement(styles, f"{{{S}}}fonts", count="1")
    font = ET.SubElement(fonts, f"{{{S}}}font"); ET.SubElement(font, f"{{{S}}}sz", val="11"); ET.SubElement(font, f"{{{S}}}name", val="Calibri")
    fills = ET.SubElement(styles, f"{{{S}}}fills", count="2")
    for pattern in ("none", "gray125"): ET.SubElement(ET.SubElement(fills, f"{{{S}}}fill"), f"{{{S}}}patternFill", patternType=pattern)
    borders = ET.SubElement(styles, f"{{{S}}}borders", count="1"); ET.SubElement(borders, f"{{{S}}}border")
    base = ET.SubElement(styles, f"{{{S}}}cellStyleXfs", count="1"); ET.SubElement(base, f"{{{S}}}xf", numFmtId="0", fontId="0", fillId="0", borderId="0")
    xfs = ET.SubElement(styles, f"{{{S}}}cellXfs", count="4")
    for identity in (0, 1, 164, 2): ET.SubElement(xfs, f"{{{S}}}xf", numFmtId=str(identity), fontId="0", fillId="0", borderId="0", xfId="0")
    cell_styles = ET.SubElement(styles, f"{{{S}}}cellStyles", count="1"); ET.SubElement(cell_styles, f"{{{S}}}cellStyle", name="Normal", xfId="0", builtinId="0")
    sheet = _element(f"{{{S}}}worksheet")
    ET.SubElement(sheet, f"{{{S}}}dimension", ref="A1:D6")
    data = ET.SubElement(sheet, f"{{{S}}}sheetData")
    row_elements = {}
    def cell(address, value, *, style=0, formula=None):
        row_number = re.search(r"[0-9]+$", address).group()
        row = row_elements.get(row_number)
        if row is None: row = row_elements[row_number] = ET.SubElement(data, f"{{{S}}}row", r=row_number)
        kind = "n" if isinstance(value, Decimal) else "inlineStr"
        result = ET.SubElement(row, f"{{{S}}}c", r=address, t=kind, s=str(style))
        if formula is not None: ET.SubElement(result, f"{{{S}}}f").text = formula
        if kind == "n": ET.SubElement(result, f"{{{S}}}v").text = format(value, "f")
        else: ET.SubElement(ET.SubElement(result, f"{{{S}}}is"), f"{{{S}}}t").text = value
    for column, label in zip("ABCD", ("商品", "数量", "单价", "金额")): cell(column + "1", label)
    for row_number, (item, quantity, price, amount) in enumerate(rows, 2):
        cell(f"A{row_number}", item); cell(f"B{row_number}", quantity, style=1); cell(f"C{row_number}", price, style=2)
        cell(f"D{row_number}", amount, style=3, formula=_FORMULAS[f"D{row_number}"])
    cell("A5", "合计"); cell("D5", total, style=3, formula=_FORMULAS["D5"])
    cell("A6", "字面示例"); cell("B6", LITERAL)
    parts = {"[Content_Types].xml": types, "_rels/.rels": root_rels, "xl/workbook.xml": workbook,
             "xl/_rels/workbook.xml.rels": relationships, "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": sheet}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items(): archive.writestr(name, _bytes(value))
    return output.getvalue()


def write_reference_outputs(workspace, case):
    """Only for offline oracle checks; never substitute for a model output."""
    _case(case); root = _root(workspace)
    rows, total = _reference(_read(root, SOURCE, MAX_SOURCE))
    target = root / OUTPUT
    if target.exists() or target.is_symlink(): _read(root, OUTPUT, MAX_ARTIFACT)
    target.write_bytes(_reference_package(rows, total))


def _mutant(raw, name):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as written:
        for item in archive.infolist():
            content = archive.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml" and name != "external_relationship":
                root = ET.fromstring(content)
                cells = {cell.get("r"): cell for cell in root.findall(f".//{{{S}}}c")}
                if name == "wrong_cache": cells["D2"].find(f"{{{S}}}v").text = "999"
                elif name == "formula_to_literal":
                    cells["D2"].remove(cells["D2"].find(f"{{{S}}}f"))
                elif name == "wrong_format": cells["D2"].set("s", "0")
                elif name == "literal_to_formula":
                    cell = cells["B6"]; cell.remove(cell.find(f"{{{S}}}is")); cell.set("t", "n")
                    ET.SubElement(cell, f"{{{S}}}f").text = "SUM(D2:D4)"; ET.SubElement(cell, f"{{{S}}}v").text = "74.99"
                content = _bytes(root)
            elif item.filename == "xl/_rels/workbook.xml.rels" and name == "external_relationship":
                root = ET.fromstring(content)
                ET.SubElement(root, f"{{{P}}}Relationship", Id="external", Type=R + "/externalLink", TargetMode="External", Target="https://example.invalid/workbook.xlsx")
                content = _bytes(root)
            written.writestr(item, content)
    return output.getvalue()


def oracle_selfchecks():
    """Repeatable private reference/negative checks, with no caller paths leaked."""
    checks = []
    with tempfile.TemporaryDirectory(prefix="aura-xlsx-oracle-selfcheck-") as folder:
        root = Path(folder)
        (root / SOURCE).write_bytes(SOURCE_TEXT.encode("utf-8"))
        write_reference_outputs(root, CASE)
        raw = (root / OUTPUT).read_bytes()
        result = evaluate(root, CASE)
        checks.append({"case": "reference", "passed": result["passed"], "issues": result["issues"]})
        for name in ("wrong_cache", "formula_to_literal", "wrong_format", "literal_to_formula", "external_relationship"):
            (root / OUTPUT).write_bytes(_mutant(raw, name))
            result = evaluate(root, CASE)
            checks.append({"case": name, "passed": result["passed"] is False and bool(result["issues"]), "issues": result["issues"]})
    return {"passed": all(item["passed"] for item in checks), "scope": "offline_oracle_reference_and_negative_guards",
            "model_calls": 0, "real_model_success": False, "checks": checks}
