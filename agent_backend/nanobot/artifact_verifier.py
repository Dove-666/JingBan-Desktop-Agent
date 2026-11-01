"""Read-only checks for workspace artifacts, independent of model assertions.

Passing a file/format check is deliberately narrower than verifying a user's
whole goal. Standard aggregate JSON receives arithmetic consistency checks;
``csv_summary`` additionally recomputes against an identified source file.
No file contents, model code, macros or archive members are executed.
"""
from __future__ import annotations

import csv
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

from safe_files import MAX_FILE_BYTES, WorkspaceFiles


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_KINDS = frozenset({"auto", "text", "json", "csv", "csv_summary", "office", "docx", "xlsx", "pptx"})
_OFFICE = {
    "docx": ("word/document.xml", "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document"),
    "xlsx": ("xl/workbook.xml", "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook"),
    "pptx": ("ppt/presentation.xml", "{http://schemas.openxmlformats.org/presentationml/2006/main}presentation"),
}
_OFFICE_SUFFIX = {".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx"}
_MAX_ARCHIVE_MEMBERS = 2000
_MAX_ARCHIVE_BYTES = 16 * MAX_FILE_BYTES
_MAX_XML_BYTES = 4 * MAX_FILE_BYTES


class _VerificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise _VerificationError("duplicate_json_key")
        result[name] = value
    return result


def _reject_constant(_):
    raise _VerificationError("nonfinite_json_number")


def _json(text):
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_float=Decimal, parse_constant=_reject_constant)
    except _VerificationError:
        raise
    except (ValueError, TypeError, RecursionError, DecimalException):
        raise _VerificationError("invalid_json") from None


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise _VerificationError("invalid_summary_number")
    if isinstance(value, str):
        value = value.strip()
        if not _NUMBER.fullmatch(value):
            raise _VerificationError("invalid_summary_number")
    try:
        number = Decimal(value)
        parts = number.as_tuple()
        if not number.is_finite() or len(parts.digits) > 1000 or abs(parts.exponent) > 400:
            raise _VerificationError("invalid_summary_number")
        return number
    except (ValueError, TypeError, DecimalException):
        raise _VerificationError("invalid_summary_number") from None


def _decimal_text(value):
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _check(result, code, passed, **details):
    result["checks"].append({"code": code, "passed": bool(passed), **details})
    if not passed:
        result["issues"].append(code)


class ArtifactVerifier:
    def __init__(self, workspace: Path):
        self.files = WorkspaceFiles(workspace)

    def _read(self, path):
        try:
            target = self.files.resolve(path)
        except (OSError, ValueError, RuntimeError):
            raise _VerificationError("outside_workspace") from None
        try:
            before = target.stat()
            if not target.is_file() or before.st_size > MAX_FILE_BYTES:
                raise _VerificationError("file_unavailable")
            with target.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            after = target.stat()
            if (len(raw) > MAX_FILE_BYTES
                    or (before.st_size, before.st_mtime_ns, before.st_ino)
                    != (after.st_size, after.st_mtime_ns, after.st_ino)
                    or self.files.resolve(path) != target):
                raise _VerificationError("file_changed")
            return target, raw
        except _VerificationError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise _VerificationError("file_unavailable") from None

    def verify(self, path: str, kind="auto", source_path="", value_column="", group_by="", delimiter=",") -> dict:
        """Return only observed checks, never a blanket claim of task success."""
        result = {"passed": False, "kind": kind if isinstance(kind, str) else "invalid",
                  "scope": "file_readback", "sha256": "", "bytes": 0,
                  "checks": [], "issues": [], "task_fully_verified": False}
        try:
            if not isinstance(kind, str) or kind not in _KINDS or not all(isinstance(value, str) for value in (path, source_path, value_column, group_by, delimiter)):
                raise _VerificationError("invalid_options")
            target, raw = self._read(path)
            _check(result, "workspace_file_read", True)
            result.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            if kind == "auto":
                kind = _OFFICE_SUFFIX.get(target.suffix.lower(),
                    {".json": "json", ".csv": "csv"}.get(target.suffix.lower(), "text"))
            elif kind == "office":
                kind = _OFFICE_SUFFIX.get(target.suffix.lower())
                if kind is None:
                    raise _VerificationError("unsupported_office_format")
            result["kind"] = kind
            if kind in _OFFICE:
                result["scope"] = "office_package_structure_only"
                self._office(raw, kind, result)
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeError:
                    raise _VerificationError("invalid_utf8") from None
                _check(result, "utf8_decode", True)
                if not text.strip() or "\0" in text:
                    raise _VerificationError("empty_or_binary_text")
                _check(result, "nonempty_text", True)
                if kind in {"json", "csv_summary"}:
                    value = _json(text)
                    result["scope"] = "json_syntax_only"
                    _check(result, "strict_json_parse", True)
                    if (isinstance(value, dict) and "category_totals" in value
                            and not (kind == "csv_summary" and not group_by)):
                        result["scope"] = "json_syntax_and_summary_consistency"
                        self._summary(value, result)
                    if kind == "csv_summary":
                        result["scope"] = "csv_summary_against_current_source"
                        self._csv_summary(value, source_path, value_column, group_by, delimiter, result)
                elif kind == "csv":
                    result["scope"] = "csv_structure_only"
                    self._csv(text, result)
            # A source/target changed after parsing is not a verified snapshot.
            _, final = self._read(path)
            _check(result, "artifact_unchanged_during_check", hashlib.sha256(final).hexdigest() == result["sha256"])
        except _VerificationError as error:
            _check(result, error.code, False)
        except (TypeError, ValueError, OSError, RuntimeError, RecursionError):
            _check(result, "verification_unavailable", False)
        result["passed"] = bool(result["checks"]) and all(item["passed"] for item in result["checks"])
        return result

    @staticmethod
    def _summary(value, result):
        groups = value.get("category_totals")
        if not isinstance(groups, dict) or len(groups) > 1000 or "total" not in value:
            raise _VerificationError("invalid_summary_structure")
        amounts = [_decimal(amount) for amount in groups.values()]
        total = _decimal(value["total"])
        if not groups:
            # An ungrouped aggregate legitimately has no category breakdown.
            # Its total requires source evidence; empty categories alone do
            # not establish that the total must be zero.
            result["scope"] = "json_syntax_only"
            _check(result, "summary_total_is_decimal", True)
            return
        try:
            with localcontext() as context:
                context.prec = 2000
                context.traps[Inexact] = context.traps[Rounded] = True
                expected = sum(amounts, Decimal(0))
        except DecimalException:
            raise _VerificationError("invalid_summary_number") from None
        _check(result, "summary_total_matches_categories", expected == total,
               expected=_decimal_text(expected), actual=_decimal_text(total))

    def _csv_summary(self, value, source_path, value_column, group_by, delimiter, result):
        if not source_path or not value_column or not isinstance(value, dict):
            raise _VerificationError("csv_summary_requires_source_and_value_column")
        from data_tools import DataToolError, DataTools
        try:
            evidence = json.loads(DataTools(self.files.root).analyze_csv(source_path, value_column, group_by, delimiter))
        except DataToolError:
            raise _VerificationError("csv_source_unavailable_or_invalid") from None
        result["source_sha256"] = evidence["source_sha256"]
        _, source = self._read(source_path)
        _check(result, "csv_source_unchanged_during_check",
               hashlib.sha256(source).hexdigest() == evidence["source_sha256"])
        if "source_sha256" in value:
            _check(result, "declared_source_sha256_matches", value["source_sha256"] == evidence["source_sha256"])
        if not {"category_totals", "total", "record_count"}.issubset(value):
            raise _VerificationError("invalid_summary_structure")
        groups = value["category_totals"]
        if not isinstance(groups, dict) or len(groups) > 1000:
            raise _VerificationError("invalid_summary_structure")
        expected_groups = {name: _decimal(amount) for name, amount in evidence["category_totals"].items()}
        actual_groups = {name: _decimal(amount) for name, amount in groups.items()}
        _check(result, "csv_category_totals_match", actual_groups == expected_groups,
               expected={name: _decimal_text(amount) for name, amount in expected_groups.items()},
               actual={name: _decimal_text(amount) for name, amount in actual_groups.items()})
        expected_total, actual_total = _decimal(evidence["total"]), _decimal(value["total"])
        _check(result, "csv_grand_total_matches", actual_total == expected_total,
               expected=_decimal_text(expected_total), actual=_decimal_text(actual_total))
        count = value["record_count"]
        _check(result, "csv_record_count_matches", type(count) is int and count == evidence["record_count"],
               expected=evidence["record_count"], actual=count if type(count) is int else "not_integer")

    @staticmethod
    def _csv(text, result):
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        try:
            header = next(reader)
            names = [name.strip() for name in header]
            if not names or len(names) > 100 or any(not name for name in names) or len(set(names)) != len(names):
                raise _VerificationError("invalid_csv_header")
            count = 0
            for row in reader:
                if not row or all(not cell.strip() for cell in row):
                    continue
                count += 1
                if count > 10000 or any(len(cell) > 4096 for cell in row):
                    raise _VerificationError("csv_structure_limit")
                if len(row) != len(header):
                    raise _VerificationError("invalid_csv_column_count")
        except (StopIteration, csv.Error):
            raise _VerificationError("invalid_csv_structure") from None
        _check(result, "csv_header_and_columns", True, columns=len(header), records=count)

    @staticmethod
    def _office(raw, kind, result):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                names = [item.filename for item in entries]
                if (len(entries) > _MAX_ARCHIVE_MEMBERS or len(set(names)) != len(names)
                        or sum(item.file_size for item in entries) > _MAX_ARCHIVE_BYTES
                        or any(item.flag_bits & 1 for item in entries)
                        or any(name.startswith(("/", "\\")) or "\\" in name or ".." in PurePosixPath(name).parts for name in names)):
                    raise _VerificationError("unsafe_or_oversized_office_package")
                main_path, main_tag = _OFFICE[kind]
                required = {"[Content_Types].xml": "{http://schemas.openxmlformats.org/package/2006/content-types}Types",
                            "_rels/.rels": "{http://schemas.openxmlformats.org/package/2006/relationships}Relationships",
                            main_path: main_tag}
                _check(result, "office_required_parts", all(name in names for name in required))
                if result["issues"]:
                    return
                for name, expected_tag in required.items():
                    if archive.getinfo(name).file_size > _MAX_XML_BYTES:
                        raise _VerificationError("unsafe_or_oversized_office_package")
                    xml = archive.read(name)
                    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                        raise _VerificationError("unsafe_office_xml")
                    try:
                        root = ET.fromstring(xml)
                    except ET.ParseError:
                        raise _VerificationError("invalid_office_xml") from None
                    if root.tag != expected_tag:
                        raise _VerificationError("invalid_office_xml")
                _check(result, "office_required_xml", True)
                _check(result, "office_archive_crc", archive.testzip() is None)
        except _VerificationError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError, ValueError,
                NotImplementedError, EOFError, zlib.error):
            raise _VerificationError("invalid_office_package") from None
