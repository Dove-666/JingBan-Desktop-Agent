"""Independent artifact facts; no model, network, GUI or user files."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from artifact_verifier import ArtifactVerifier
from data_tools import DataTools
from safe_files import MAX_FILE_BYTES


class ArtifactVerifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-artifact-")
        self.root = Path(self.directory.name)
        self.verifier = ArtifactVerifier(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, path, content):
        target = self.root / path
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_bytes(content)
        return target

    def summary(self, *, total="1233.39", groups=None, count=20, **extra):
        data = {"category_totals": groups if groups is not None else {"设备": "849.44", "资料": "300.15", "交通": "83.80"},
                "total": total, "record_count": count, **extra}
        self.write("summary.json", json.dumps(data, ensure_ascii=False))
        return data

    def source(self):
        # Same total as the previous real-model regression, compact fixture.
        rows = [("设备", "849.44"), ("资料", "300.15"), ("交通", "83.80")]
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["类别", "金额"])
        writer.writerows(rows)
        return self.write("source.csv", stream.getvalue())

    def office(self, kind="xlsx", *, missing="", invalid="", extra=None, compression=zipfile.ZIP_STORED):
        mapping = {
            "xlsx": ("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'),
            "docx": ("word/document.xml", '<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'),
            "pptx": ("ppt/presentation.xml", '<presentation xmlns="http://schemas.openxmlformats.org/presentationml/2006/main"/>'),
        }
        main, xml = mapping[kind]
        parts = {"[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
                 "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>', main: xml}
        if missing:
            parts.pop(missing)
        if invalid:
            parts[main] = invalid
        if extra:
            parts.update(extra)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=compression) as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        return self.write("artifact." + kind, output.getvalue())

    def test_text_readback_is_scoped_and_has_current_hash_without_blanket_goal_claim(self):
        path = self.write("note.txt", "中文🙂\n真实文本")
        before = path.read_bytes()
        result = self.verifier.verify("note.txt")
        self.assertTrue(result["passed"])
        self.assertEqual(result["scope"], "file_readback")
        self.assertFalse(result["task_fully_verified"])
        self.assertEqual(result["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["bytes"], len(before))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(p.name for p in self.root.iterdir()), {"note.txt"})

    def test_changed_file_gets_new_hash_on_next_verification(self):
        path = self.write("note.txt", "version one")
        old = self.verifier.verify("note.txt")
        path.write_text("version two", encoding="utf-8")
        new = self.verifier.verify("note.txt")
        self.assertTrue(new["passed"])
        self.assertNotEqual(old["sha256"], new["sha256"])

    def test_empty_binary_oversized_and_missing_files_fail_clearly(self):
        cases = (("empty.txt", b" \n", "empty_or_binary_text"),
                 ("binary.txt", b"abc\0def", "empty_or_binary_text"),
                 ("encoding.txt", b"\xff\xfe", "invalid_utf8"),
                 ("large.txt", b"x" * (MAX_FILE_BYTES + 1), "file_unavailable"))
        for name, raw, issue in cases:
            with self.subTest(name=name):
                self.write(name, raw)
                result = self.verifier.verify(name)
                self.assertFalse(result["passed"])
                self.assertIn(issue, result["issues"])
        self.assertIn("file_unavailable", self.verifier.verify("missing.txt")["issues"])

    def test_outside_workspace_never_echoes_paths(self):
        with tempfile.TemporaryDirectory(prefix="aura-private-") as directory:
            private = Path(directory) / "private-secret-name.txt"
            private.write_text("private body", encoding="utf-8")
            for value in (str(private), "../outside.txt"):
                result = self.verifier.verify(value)
                encoded = json.dumps(result)
                self.assertFalse(result["passed"])
                self.assertIn("outside_workspace", result["issues"])
                self.assertNotIn(str(private), encoded)
                self.assertNotIn("private body", encoded)
                self.assertNotIn("private-secret-name", encoded)

    def test_symlink_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="aura-outside-") as directory:
            outside = Path(directory) / "secret.txt"
            outside.write_text("not returned", encoding="utf-8")
            link = self.root / "link.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                if sys.platform != "win32":
                    self.skipTest("This account cannot create a symlink")
                # Windows directory junctions need no Developer Mode/admin
                # privilege and exercise the same resolve-boundary check.
                import _winapi
                junction = self.root / "outside-junction"
                _winapi.CreateJunction(str(outside.parent), str(junction))
                link = junction / outside.name
            result = self.verifier.verify(str(link))
            self.assertFalse(result["passed"])
            self.assertIn("outside_workspace", result["issues"])

    def test_plain_json_passes_syntax_only_not_arbitrary_semantics(self):
        self.write("data.json", '{"claim":"not checked against reality","nested":[1,2.1]}')
        result = self.verifier.verify("data.json")
        self.assertTrue(result["passed"])
        self.assertEqual(result["scope"], "json_syntax_only")
        self.assertFalse(result["task_fully_verified"])

    def test_json_is_strict_for_duplicates_nonfinite_and_bad_syntax(self):
        cases = ('{"a":1,"a":2}', '{"outer":{"x":1,"x":2}}',
                 '{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}', '{broken}', '[1,]')
        for body in cases:
            with self.subTest(body=body):
                self.write("bad.json", body)
                result = self.verifier.verify("bad.json")
                self.assertFalse(result["passed"])
                self.assertTrue(set(result["issues"]) & {"duplicate_json_key", "nonfinite_json_number", "invalid_json"})

    def test_previous_real_model_error_is_rejected_despite_valid_json_and_readback(self):
        self.summary(total="1133.39")
        result = self.verifier.verify("summary.json")
        self.assertFalse(result["passed"])
        check = next(c for c in result["checks"] if c["code"] == "summary_total_matches_categories")
        self.assertEqual(check["expected"], "1233.39")
        self.assertEqual(check["actual"], "1133.39")
        self.assertEqual(result["scope"], "json_syntax_and_summary_consistency")

    def test_decimal_sum_avoids_binary_float_false_mismatch(self):
        self.summary(total=0.3, groups={"a": 0.1, "b": 0.2}, count=2)
        result = self.verifier.verify("summary.json")
        self.assertTrue(result["passed"])
        self.assertFalse(result["task_fully_verified"])

    def test_invalid_summary_values_and_missing_total_fail(self):
        for value in (True, None, [], "NaN", "1e9999999", "" , "$20"):
            with self.subTest(value=value):
                self.summary(total=value)
                self.assertFalse(self.verifier.verify("summary.json")["passed"])
        self.write("summary.json", '{"category_totals":{"a":1}}')
        self.assertIn("invalid_summary_structure", self.verifier.verify("summary.json")["issues"])

    def test_csv_format_does_not_guess_numbers_or_claim_math(self):
        self.write("text.csv", 'name,description\n鲸鱼娘,"hello, world"\n')
        result = self.verifier.verify("text.csv")
        self.assertTrue(result["passed"])
        self.assertEqual(result["scope"], "csv_structure_only")
        self.assertFalse(result["task_fully_verified"])

    def test_bad_csv_header_columns_or_quoting_fails(self):
        for body in ('a,a\n1,2\n', 'a,\n1,2\n', 'a,b\n1\n', 'a,b\n1,2,3\n', 'a,b\n"not closed,2'):
            with self.subTest(body=body):
                self.write("bad.csv", body)
                self.assertFalse(self.verifier.verify("bad.csv")["passed"])

    def test_csv_summary_recomputes_exact_totals_and_counts_against_source(self):
        source = self.source()
        self.summary(count=3)
        before = source.read_bytes()
        result = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["scope"], "csv_summary_against_current_source")
        self.assertEqual(source.read_bytes(), before)

    def test_csv_summary_catches_consistent_but_wrong_source_results(self):
        self.source()
        self.summary(total="3", groups={"设备":"1", "资料":"1", "交通":"1"}, count=99)
        result = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别")
        self.assertFalse(result["passed"])
        self.assertIn("csv_category_totals_match", result["issues"])
        self.assertIn("csv_grand_total_matches", result["issues"])
        self.assertIn("csv_record_count_matches", result["issues"])

    def test_ungrouped_csv_summary_verifies_total_without_inventing_categories(self):
        self.source()
        self.summary(total="1233.39", groups={}, count=3)
        result = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额")
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["task_fully_verified"])

    def test_empty_categories_do_not_invent_a_zero_total_without_source_evidence(self):
        self.summary(total="1233.39", groups={}, count=3)
        result = self.verifier.verify("summary.json")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["scope"], "json_syntax_only")
        self.assertFalse(result["task_fully_verified"])

    def test_explicit_csv_summary_uses_the_analyzed_source_delimiter(self):
        self.write("source.csv", "类别;金额\n设备;849.44\n资料;300.15\n交通;83.80\n")
        self.summary(count=3)
        result = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别", ";")
        self.assertTrue(result["passed"], result)
        wrong_delimiter = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别", ",")
        self.assertFalse(wrong_delimiter["passed"])

    def test_csv_summary_tracks_source_changes_even_when_totals_stay_equal(self):
        source = self.source()
        old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        self.summary(count=3, source_sha256=old_hash)
        first = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别")
        self.assertTrue(first["passed"])
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        second = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别")
        self.assertFalse(second["passed"])
        self.assertNotEqual(second["source_sha256"], old_hash)
        self.assertIn("declared_source_sha256_matches", second["issues"])

    def test_csv_summary_rejects_missing_source_column_or_boolean_record_count(self):
        self.source()
        self.summary(count=True)
        result = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别")
        self.assertIn("csv_record_count_matches", result["issues"])
        for source, column in (("", "金额"), ("source.csv", ""), ("missing.csv", "金额"), ("source.csv", "not-a-column")):
            with self.subTest(source=source, column=column):
                result = self.verifier.verify("summary.json", "csv_summary", source, column, "类别")
                self.assertFalse(result["passed"])

    def test_source_mutation_after_calculation_is_detected(self):
        source = self.source()
        self.summary(count=3)
        original = DataTools.analyze_csv
        def changing(instance, *args, **kwargs):
            value = original(instance, *args, **kwargs)
            source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            return value
        with patch.object(DataTools, "analyze_csv", changing):
            result = self.verifier.verify("summary.json", "csv_summary", "source.csv", "金额", "类别")
        self.assertFalse(result["passed"])
        self.assertIn("csv_source_unchanged_during_check", result["issues"])

    def test_target_mutation_during_verification_is_detected(self):
        target = self.write("text.csv", "a,b\n1,2\n")
        original = ArtifactVerifier._csv
        def changing(text, result):
            original(text, result)
            target.write_text("a,b\n3,4\n", encoding="utf-8")
        with patch.object(ArtifactVerifier, "_csv", staticmethod(changing)):
            result = self.verifier.verify("text.csv")
        self.assertFalse(result["passed"])
        self.assertIn("artifact_unchanged_during_check", result["issues"])

    def test_office_containers_check_required_xml_and_crc_with_limited_scope(self):
        for kind in ("xlsx", "docx", "pptx"):
            with self.subTest(kind=kind):
                target = self.office(kind)
                before = target.read_bytes()
                result = self.verifier.verify(target.name)
                self.assertTrue(result["passed"], result)
                self.assertEqual(result["scope"], "office_package_structure_only")
                self.assertFalse(result["task_fully_verified"])
                self.assertEqual(before, target.read_bytes())

    def test_text_renamed_to_xlsx_is_not_an_office_document(self):
        self.write("fake.xlsx", "This is not a spreadsheet")
        result = self.verifier.verify("fake.xlsx")
        self.assertFalse(result["passed"])
        self.assertIn("invalid_office_package", result["issues"])

    def test_office_required_parts_namespaces_and_xml_are_checked(self):
        self.office(missing="xl/workbook.xml")
        self.assertIn("office_required_parts", self.verifier.verify("artifact.xlsx")["issues"])
        for body in ("<broken", "<workbook/>", '<!DOCTYPE workbook [<!ENTITY x "bad">]><workbook>&x;</workbook>'):
            with self.subTest(body=body):
                self.office(invalid=body)
                self.assertFalse(self.verifier.verify("artifact.xlsx")["passed"])

    def test_office_zip_bombs_and_unsafe_member_paths_are_bounded(self):
        self.office(extra={"huge.bin": b"x" * (16 * MAX_FILE_BYTES + 1)}, compression=zipfile.ZIP_DEFLATED)
        self.assertLess((self.root / "artifact.xlsx").stat().st_size, MAX_FILE_BYTES)
        result = self.verifier.verify("artifact.xlsx")
        self.assertFalse(result["passed"])
        self.assertIn("unsafe_or_oversized_office_package", result["issues"])
        self.office(extra={"../outside.txt": "not extracted"})
        self.assertIn("unsafe_or_oversized_office_package", self.verifier.verify("artifact.xlsx")["issues"])

    def test_bad_zip_crc_is_not_reported_as_pass(self):
        target = self.office()
        # Flip a stored payload byte outside required XML; all entries are CRC checked.
        self.office(extra={"extra.bin": "ABCDEF"})
        raw = bytearray(target.read_bytes())
        marker = raw.index(b"ABCDEF")
        raw[marker] = ord("Z")
        target.write_bytes(raw)
        result = self.verifier.verify("artifact.xlsx")
        self.assertFalse(result["passed"])
        self.assertTrue(set(result["issues"]) & {"office_archive_crc", "invalid_office_package"})

    def test_invalid_options_are_static_and_never_raise(self):
        self.write("note.txt", "text")
        for kind in ("unknown", [], {}, None):
            result = self.verifier.verify("note.txt", kind)
            self.assertFalse(result["passed"])
            self.assertIn("invalid_options", result["issues"])
        self.assertIn("invalid_options", self.verifier.verify(None)["issues"])


if __name__ == "__main__":
    unittest.main()
