"""Optional source-evidence contract for deterministic CSV summaries.

It is enabled by the host by default, never selectable by a model request.
The generic harness owns lifecycle and artifacts; this contract owns CSV rules.
"""
from __future__ import annotations
import hashlib
import json
from safe_files import MAX_FILE_BYTES


class CsvSourceContract:
    name = "csv_source"

    def verify(self, harness, path, request, report, *, refresh=False):
        try:
            if path.casefold().endswith(".json"):
                with harness.files.resolve(path).open("rb") as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                target = json.loads(raw.decode("utf-8-sig")) if len(raw) <= MAX_FILE_BYTES else None
            else:
                target = None
        except (ValueError, OSError, UnicodeError):
            target = None
        is_summary = isinstance(target, dict) and {"category_totals", "total", "record_count"} <= target.keys()
        source_path = request.get("source_path", "")
        analysis = None
        selected_request = None
        if source_path:
            try:
                relative_source = harness._relative(source_path)
            except (ValueError, OSError):
                return harness._reject(report, "SOURCE_OUTSIDE_WORKSPACE")
            candidates = [(key, value) for key, value in harness.analyses.items() if key[0] == relative_source]
            if request.get("value_column"):
                selected_key = (relative_source, request["value_column"], request.get("group_by", ""),
                                request.get("delimiter", ","))
                analysis = harness.analyses.get(selected_key)
                selected_request = {"kind": "csv_summary", "source_path": source_path,
                    "value_column": request["value_column"], "group_by": request.get("group_by", ""),
                    "delimiter": request.get("delimiter", ",")}
            elif len(candidates) == 1:
                analysis = candidates[0][1]
            elif candidates:
                return harness._reject(report, "SOURCE_CONFIGURATION_REQUIRED")
        elif is_summary and len(harness.analyses) == 1:
            analysis = next(iter(harness.analyses.values()))
        elif is_summary and len(harness.analyses) > 1:
            report = harness._reject(report, "SOURCE_SELECTION_REQUIRED")
        elif is_summary and harness.csv_reads:
            report = harness._reject(report, "SOURCE_ANALYSIS_REQUIRED")
        # A weak explicit check cannot downgrade already available evidence.
        if is_summary and selected_request:
            report = harness.verifier.verify(path, **selected_request)
        elif is_summary and analysis:
            report = harness.verifier.verify(path, kind="csv_summary", source_path=analysis["source_path"],
                value_column=analysis["value_column"], group_by=analysis["group_by"], delimiter=analysis["delimiter"])
        elif is_summary and source_path:
            report = harness.verifier.verify(path, kind="csv_summary", source_path=source_path,
                value_column=request.get("value_column", ""), group_by=request.get("group_by", ""),
                delimiter=request.get("delimiter", ","))
        if analysis and (is_summary or request.get("kind") == "csv_summary"):
            source = harness.files.resolve(analysis["source_path"])
            try:
                with source.open("rb") as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                unchanged = (len(raw) <= MAX_FILE_BYTES and
                             hashlib.sha256(raw).hexdigest() == analysis["source_sha256"])
            except OSError:
                unchanged = False
            if not unchanged:
                report = harness._reject(report, "SOURCE_CHANGED_AFTER_ANALYSIS")
        return report
