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
import stat
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

from safe_files import MAX_DOCUMENT_BYTES, MAX_FILE_BYTES, WorkspaceFiles


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_KINDS = frozenset({"auto", "text", "json", "csv", "csv_summary", "office", "docx", "xlsx", "pptx", "pdf", "zip"})
_OFFICE = {
    "docx": ("word/document.xml", "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document"),
    "xlsx": ("xl/workbook.xml", "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook"),
    "pptx": ("ppt/presentation.xml", "{http://schemas.openxmlformats.org/presentationml/2006/main}presentation"),
}
_OFFICE_SUFFIX = {".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx"}
_MAX_ARCHIVE_MEMBERS = 2000
_MAX_ARCHIVE_BYTES = 16 * MAX_FILE_BYTES
_MAX_XML_BYTES = 4 * MAX_FILE_BYTES
_MAX_ZIP_PUBLIC_MEMBERS = 64
_WINDOWS_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *("COM" + value for value in "123456789¹²³"), *("LPT" + value for value in "123456789¹²³")})


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


def _zip_directory(raw):
    """Bound the real central directory BEFORE ZipFile allocates its filelist.

    EOCD's declared entry count alone is insufficient: Python walks directory
    bytes rather than trusting that count. Support bounded single-disk ZIP64
    metadata as well, without following offsets outside these owned bytes.
    """
    if not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise _VerificationError("invalid_zip_structure")
    end = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise _VerificationError("invalid_zip_structure")
    disk, directory_disk, on_disk, count, size, offset, comment = struct.unpack_from("<HHHHIIH", raw, end + 4)
    if disk or directory_disk or on_disk != count or end + 22 + comment != len(raw):
        raise _VerificationError("invalid_zip_structure")
    directory_end = end
    if end >= 20 and raw[end - 20:end - 16] == b"PK\x06\x07":
        disk64, record_at, disks = struct.unpack_from("<IQI", raw, end - 16)
        if disk64 or disks != 1 or record_at + 56 > end - 20 or raw[record_at:record_at + 4] != b"PK\x06\x06":
            raise _VerificationError("invalid_zip_structure")
        record_size = struct.unpack_from("<Q", raw, record_at + 4)[0]
        if not 44 <= record_size <= 4096 or record_at + 12 + record_size != end - 20:
            raise _VerificationError("invalid_zip_structure")
        _, _, disk64, directory_disk64, on_disk64, count64, size64, offset64 = struct.unpack_from("<HHIIQQQQ", raw, record_at + 12)
        if (disk64 or directory_disk64 or on_disk64 != count64
                or count != 0xffff and count != count64
                or on_disk != 0xffff and on_disk != on_disk64
                or size != 0xffffffff and size != size64
                or offset != 0xffffffff and offset != offset64):
            raise _VerificationError("invalid_zip_structure")
        count, size, offset, directory_end = count64, size64, offset64, record_at
    if count > _MAX_ARCHIVE_MEMBERS:
        raise _VerificationError("zip_member_limit")
    if offset > directory_end or size != directory_end - offset or size < count * 46:
        raise _VerificationError("invalid_zip_structure")
    position, observed = offset, 0
    while position < directory_end:
        observed += 1
        if observed > _MAX_ARCHIVE_MEMBERS:
            raise _VerificationError("zip_member_limit")
        if position + 46 > directory_end or raw[position:position + 4] != b"PK\x01\x02":
            raise _VerificationError("invalid_zip_structure")
        names, extra, comments = struct.unpack_from("<HHH", raw, position + 28)
        if not 1 <= names <= 2048:
            raise _VerificationError("unsafe_zip_member")
        position += 46 + names + extra + comments
        if position > directory_end:
            raise _VerificationError("invalid_zip_structure")
    if observed != count or (count and not raw.startswith(b"PK\x03\x04")) or (not count and offset):
        raise _VerificationError("invalid_zip_structure")
    return count, offset


def _zip_member_parts(item):
    name = item.filename
    if (not name or item.orig_filename != name or len(name) > 512 or name.startswith("/")
            or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in name)
            or any(character in name for character in '\\:<>"|?*')):
        raise _VerificationError("unsafe_zip_member")
    directory = item.is_dir()
    parts = (name[:-1] if directory else name).split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " "))
           or part.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES for part in parts):
        raise _VerificationError("unsafe_zip_member")
    mode = stat.S_IFMT(item.external_attr >> 16)
    if (mode not in {0, stat.S_IFREG, stat.S_IFDIR} or item.external_attr & 0x400
            or mode == stat.S_IFDIR and not directory
            or item.external_attr & 0x10 and not directory
            or directory and item.file_size):
        raise _VerificationError("unsafe_zip_member")
    return tuple(part.casefold() for part in parts), directory


def _zip_data_bounds(raw, item, directory_start):
    """Check local metadata and return the exact compressed-data byte range."""
    local = item.header_offset
    if not 0 <= local <= directory_start - 30 or raw[local:local + 4] != b"PK\x03\x04":
        raise _VerificationError("invalid_zip_structure")
    _, flags, method, _, _, crc, compressed, size, names, extras = struct.unpack_from("<5H3I2H", raw, local + 4)
    start = local + 30 + names + extras
    end = start + item.compress_size
    if ((flags, method) != (item.flag_bits, item.compress_type) or not names
            or start > directory_start or end > directory_start):
        raise _VerificationError("invalid_zip_structure")
    name = raw[local + 30:local + 30 + names].decode("utf-8" if flags & 0x800 else "cp437")
    if name != item.orig_filename:
        raise _VerificationError("invalid_zip_structure")
    position, zip64 = local + 30 + names, None
    while position < start:
        if position + 4 > start:
            raise _VerificationError("invalid_zip_structure")
        kind, length = struct.unpack_from("<HH", raw, position)
        position += 4
        if position + length > start:
            raise _VerificationError("invalid_zip_structure")
        if kind == 1:
            if zip64 is not None:
                raise _VerificationError("invalid_zip_structure")
            zip64 = raw[position:position + length]
        position += length
    large = compressed == 0xffffffff or size == 0xffffffff
    if large:
        position = 0
        for field in ("size", "compressed"):
            value = size if field == "size" else compressed
            if value != 0xffffffff:
                continue
            if zip64 is None or position + 8 > len(zip64):
                raise _VerificationError("invalid_zip_structure")
            value = struct.unpack_from("<Q", zip64, position)[0]
            position += 8
            if field == "size":
                size = value
            else:
                compressed = value
    descriptor = bool(flags & 8)
    if descriptor:
        if crc not in {0, item.CRC} or compressed not in {0, item.compress_size} or size not in {0, item.file_size}:
            raise _VerificationError("invalid_zip_structure")
        # A streaming writer may put zero 32-bit placeholders alongside a
        # ZIP64 extra field, then write a 64-bit descriptor. Do not infer the
        # descriptor width solely from a 0xffffffff size placeholder.
        if zip64 is not None and not large:
            if len(zip64) < 16:
                raise _VerificationError("invalid_zip_structure")
            extra_size, extra_compressed = struct.unpack_from("<QQ", zip64)
            if extra_size not in {0, item.file_size} or extra_compressed not in {0, item.compress_size}:
                raise _VerificationError("invalid_zip_structure")
        layout = "<IQQ" if large or zip64 is not None else "<III"
        length = struct.calcsize(layout)
        valid_end = None
        for prefix in (0, 4):
            if prefix and raw[end:end + 4] != b"PK\x07\x08":
                continue
            if end + prefix + length <= directory_start and struct.unpack_from(layout, raw, end + prefix) == (item.CRC, item.compress_size, item.file_size):
                valid_end = end + prefix + length
                break
        if valid_end is None:
            raise _VerificationError("invalid_zip_structure")
    else:
        if (crc, compressed, size) != (item.CRC, item.compress_size, item.file_size):
            raise _VerificationError("invalid_zip_structure")
        valid_end = end
    return start, end, valid_end


def _zip_chunks(raw, item, start, end, remaining):
    """Read a complete bounded raw stream, independent of ZipExtFile's size cap.

    ZipExtFile may truncate output to an untrusted central-directory size and
    then validate only that prefix. No prefix or simulated EOF can pass here.
    """
    maximum = min(item.file_size, remaining)
    if item.compress_type == zipfile.ZIP_STORED:
        if item.compress_size != item.file_size:
            raise _VerificationError("invalid_zip_structure")
        for position in range(start, end, 65536):
            yield raw[position:min(position + 65536, end)]
        return
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    position, produced, pending = start, 0, b""
    while True:
        if not pending and position < end:
            pending = raw[position:min(position + 65536, end)]
            position += len(pending)
        before = len(pending)
        block = decoder.decompress(pending, min(65536, maximum - produced + 1))
        pending = decoder.unconsumed_tail
        if decoder.unused_data:
            raise _VerificationError("invalid_zip_structure")
        produced += len(block)
        if produced > maximum:
            raise _VerificationError("zip_expansion_limit" if remaining < item.file_size else "invalid_zip_structure")
        if block:
            yield block
        if decoder.eof:
            if pending or position != end:
                raise _VerificationError("invalid_zip_structure")
            return
        if not block and before == len(pending):
            raise _VerificationError("invalid_zip_structure")


class ArtifactVerifier:
    def __init__(self, workspace: Path, *, files=None):
        self.files = files if files is not None else WorkspaceFiles(workspace)

    def _read(self, path, *, kind=None):
        try:
            target = self.files.resolve(path)
        except (OSError, ValueError, RuntimeError):
            raise _VerificationError("outside_workspace") from None
        try:
            before = target.stat()
            maximum = MAX_DOCUMENT_BYTES if target.suffix.lower() in _OFFICE_SUFFIX or target.suffix.lower() in {".pdf", ".zip"} or kind == "zip" else MAX_FILE_BYTES
            if not target.is_file() or before.st_size > maximum:
                raise _VerificationError("file_unavailable")
            with target.open("rb") as stream:
                raw = stream.read(maximum + 1)
            after = target.stat()
            if (len(raw) > maximum
                    or (before.st_size, before.st_mtime_ns, before.st_ino)
                    != (after.st_size, after.st_mtime_ns, after.st_ino)
                    or self.files.resolve(path) != target):
                raise _VerificationError("file_changed")
            return target, raw
        except _VerificationError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise _VerificationError("file_unavailable") from None

    def verify(self, path: str, kind="auto", source_path="", value_column="", group_by="", delimiter=",", expected_bytes=None) -> dict:
        """Return only observed checks, never a blanket claim of task success."""
        result = {"passed": False, "kind": kind if isinstance(kind, str) else "invalid",
                  "scope": "file_readback", "sha256": "", "bytes": 0,
                  "checks": [], "issues": [], "task_fully_verified": False}
        try:
            if not isinstance(kind, str) or kind not in _KINDS or not all(isinstance(value, str) for value in (path, source_path, value_column, group_by, delimiter)):
                raise _VerificationError("invalid_options")
            if expected_bytes is not None and (type(expected_bytes) is not int or not 0 <= expected_bytes <= MAX_DOCUMENT_BYTES):
                raise _VerificationError("invalid_options")
            target, raw = self._read(path, kind=kind)
            _check(result, "workspace_file_read", True)
            result.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            if expected_bytes is not None:
                _check(result, "expected_file_bytes", len(raw) == expected_bytes, expected=expected_bytes, actual=len(raw))
            if kind == "auto":
                kind = _OFFICE_SUFFIX.get(target.suffix.lower(),
                    {".json": "json", ".csv": "csv", ".pdf": "pdf", ".zip": "zip"}.get(target.suffix.lower(), "text"))
            elif kind == "office":
                kind = _OFFICE_SUFFIX.get(target.suffix.lower())
                if kind is None:
                    raise _VerificationError("unsupported_office_format")
            result["kind"] = kind
            if kind in _OFFICE:
                result["scope"] = "office_package_structure_only"
                self._office(raw, kind, result)
            elif kind == "pdf":
                self._pdf(raw, result)
            elif kind == "zip":
                result["scope"] = "zip_structure_crc_only"
                self._zip(raw, result)
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeError:
                    raise _VerificationError("invalid_utf8") from None
                _check(result, "utf8_decode", True)
                if "\0" in text or not text.strip() and expected_bytes != 0:
                    raise _VerificationError("empty_or_binary_text")
                _check(result, "expected_empty_text" if expected_bytes == 0 else "nonempty_text", True)
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
            _, final = self._read(path, kind=kind)
            _check(result, "artifact_unchanged_during_check", hashlib.sha256(final).hexdigest() == result["sha256"])
        except _VerificationError as error:
            _check(result, error.code, False)
        except (TypeError, ValueError, OSError, RuntimeError, RecursionError):
            _check(result, "verification_unavailable", False)
        result["passed"] = bool(result["checks"]) and all(item["passed"] for item in result["checks"])
        return result

    @staticmethod
    def _zip(raw, result):
        """Read bounded member bytes for CRC/hash facts, never extract/run them."""
        try:
            count, directory_start = _zip_directory(raw)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) != count:
                    raise _VerificationError("invalid_zip_structure")
                total = sum(item.file_size for item in entries)
                if total > _MAX_ARCHIVE_BYTES:
                    raise _VerificationError("zip_expansion_limit")
                seen, paths, ranges = set(), [], []
                for item in entries:
                    key, directory = _zip_member_parts(item)
                    if item.flag_bits & (1 | 0x40 | 0x2000):
                        raise _VerificationError("encrypted_zip_member")
                    if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise _VerificationError("unsupported_zip_compression")
                    if key in seen:
                        raise _VerificationError("conflicting_zip_members")
                    seen.add(key)
                    paths.append((key, directory))
                    start, end, member_end = _zip_data_bounds(raw, item, directory_start)
                    ranges.append((item, start, end, member_end))
                # Descendants are contiguous after their parent in tuple order.
                # A file therefore conflicts iff the next actual path starts
                # with its components. Keep only actual names: materializing
                # every implicit parent tuple takes quadratic space in depth.
                previous_key, previous_directory = (), True
                for key, directory in sorted(paths, key=lambda value: value[0]):
                    if not previous_directory and key[:len(previous_key)] == previous_key:
                        raise _VerificationError("conflicting_zip_members")
                    previous_key, previous_directory = key, directory
                previous_end = 0
                for item, _, _, member_end in sorted(ranges, key=lambda value: value[0].header_offset):
                    if item.header_offset < previous_end:
                        raise _VerificationError("invalid_zip_structure")
                    previous_end = member_end
                metadata, read_total = [], 0
                for item, start, end, _ in ranges:
                    digest, read, crc = hashlib.sha256(), 0, 0
                    for block in _zip_chunks(raw, item, start, end, _MAX_ARCHIVE_BYTES - read_total):
                        read += len(block)
                        read_total += len(block)
                        if read > item.file_size or read_total > _MAX_ARCHIVE_BYTES:
                            raise _VerificationError("zip_expansion_limit")
                        digest.update(block)
                        crc = zlib.crc32(block, crc)
                    if read != item.file_size:
                        raise _VerificationError("invalid_zip_structure")
                    if crc & 0xffffffff != item.CRC:
                        raise _VerificationError("invalid_zip_structure")
                    if len(metadata) < _MAX_ZIP_PUBLIC_MEMBERS:
                        metadata.append({"name": item.filename, "bytes": read, "sha256": digest.hexdigest()})
                _check(result, "zip_structure_and_members", True, members=count, uncompressed_bytes=total)
                _check(result, "zip_member_crc_readback", True)
                result.update(zip_members=metadata, zip_members_total=count,
                              zip_members_omitted=count - len(metadata))
        except _VerificationError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError, ValueError,
                UnicodeError, NotImplementedError, EOFError, zlib.error, OverflowError, struct.error):
            raise _VerificationError("invalid_zip_structure") from None

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
            evidence = json.loads(DataTools(self.files.root, files=self.files).analyze_csv(source_path, value_column, group_by, delimiter))
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
    def _pdf(raw, result):
        if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-2048:]:
            raise _VerificationError("invalid_pdf_structure")
        try:
            from pypdf import PdfReader
        except ImportError:
            raise _VerificationError("pdf_parser_unavailable") from None
        try:
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 1000:
                raise _VerificationError("unsupported_pdf_document")
            catalog = reader.trailer["/Root"]
            # Walk the parsed object graph, including page/annotation actions,
            # nested AA event dictionaries and chained Next actions. References
            # may cycle through Parent; visit identities once and bound work.
            stack, seen, count = [catalog], set(), 0
            while stack:
                value = stack.pop()
                if hasattr(value, "get_object"):
                    value = value.get_object()
                if not isinstance(value, (dict, list, tuple)) or id(value) in seen:
                    continue
                seen.add(id(value))
                count += 1
                if count > 100_000:
                    raise _VerificationError("unsupported_pdf_document")
                if isinstance(value, dict):
                    if ("/JavaScript" in value or "/JS" in value or value.get("/S") in {
                            "/JavaScript", "/Launch", "/SubmitForm", "/GoToR", "/GoToE", "/ImportData"}):
                        raise _VerificationError("active_pdf_content")
                    stack.extend(value.values())
                else:
                    stack.extend(value)
            result["scope"] = "pdf_structure_and_page_readback"
            _check(result, "pdf_pages_read", True, pages=len(reader.pages))
        except _VerificationError:
            raise
        except Exception:
            raise _VerificationError("invalid_pdf_structure") from None

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
