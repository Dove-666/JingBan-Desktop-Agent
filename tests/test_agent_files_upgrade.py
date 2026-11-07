"""Real temporary-file workflows for versioned tools and recoverable planning."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from artifact_verifier import ArtifactVerifier
from data_tools import DataTools
from safe_files import WorkspaceFiles
from task_harness import HarnessError, TaskHarness


class FileUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-files-upgrade-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.external = self.base / "selected"
        self.root.mkdir(); self.external.mkdir()
        self.files = WorkspaceFiles(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_long_single_line_json_is_completely_readable_by_versioned_pages(self):
        body = json.dumps({"prefix": "鲸鱼🙂" * 12_000, "answer": "UNIQUE_TAIL"}, ensure_ascii=False)
        self.files.write("large.json", body)
        first = json.loads(self.files.read("large.json"))
        self.assertTrue(first["truncated"])
        self.assertEqual(first["total_characters"], len(body))
        chunks, offset = [], 0
        while offset is not None:
            page = json.loads(self.files.read_page("large.json", offset=offset, limit=5678,
                                                  expected_hash=first["sha256"]))
            chunks.append(page["text"]); offset = page["next_offset"]
        self.assertEqual("".join(chunks), body)

    def test_line_pages_and_modified_file_do_not_mix_versions(self):
        self.files.write("lines.txt", "one\n二\nthree\n")
        first = json.loads(self.files.read_page("lines.txt", offset=1, limit=1, unit="lines"))
        self.assertEqual(first["text"], "二\n")
        self.assertEqual(first["next_offset"], 2)
        (self.root / "lines.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "FILE_CHANGED"):
            self.files.read_page("lines.txt", offset=2, expected_hash=first["sha256"])

    def test_search_excerpt_contains_a_long_line_tail_match(self):
        self.files.write("long.json", "x" * 25_000 + "UNIQUE_TAIL")
        match = json.loads(self.files.search("UNIQUE_TAIL"))["matches"][0]
        self.assertIn("UNIQUE_TAIL", match["text"])
        self.assertGreater(match["character_offset"], 24_000)

    def test_search_context_uses_original_offsets_after_unicode_casefold_expansion(self):
        self.files.write("unicode.txt", "ß" * 2000 + "NEEDLE")
        match = json.loads(self.files.search("needle"))["matches"][0]
        self.assertIn("NEEDLE", match["text"])
        self.assertEqual(match["character_offset"], 1840)

    def test_stale_overwrite_preserves_the_external_edit(self):
        self.files.write("shared.txt", "original")
        self.files.read("shared.txt")
        target = self.root / "shared.txt"
        target.write_text("user edit after observation", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "FILE_CHANGED"):
            self.files.write("shared.txt", "stale replacement", overwrite=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "user edit after observation")
        self.assertFalse(list(self.root.glob(".aura-write-*")))

    def test_explicit_hash_or_observation_is_required_for_existing_files(self):
        target = self.root / "existing.txt"
        target.write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "READ_BEFORE_OVERWRITE"):
            self.files.write("existing.txt", "replacement", overwrite=True)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        result = json.loads(self.files.write("existing.txt", "replacement", overwrite=True, expected_hash=digest))
        self.assertEqual(result["sha256"], hashlib.sha256(b"replacement").hexdigest())

    def test_atomic_replace_failure_keeps_original_and_removes_temporary_file(self):
        self.files.write("existing.txt", "original")
        with patch("safe_files.os.replace", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                self.files.write("existing.txt", "replacement", overwrite=True)
        self.assertEqual((self.root / "existing.txt").read_text(), "original")
        self.assertFalse(list(self.root.glob(".aura-write-*")))

    def test_targeted_edit_is_unambiguous_and_version_checked(self):
        self.files.write("code.txt", "before\nchange me\nafter\n")
        result = json.loads(self.files.edit("code.txt", "change me", "已修改"))
        self.assertEqual((self.root / "code.txt").read_text(encoding="utf-8"), "before\n已修改\nafter\n")
        self.assertEqual(result["artifact"]["sha256"], result["sha256"])
        self.files.write("repeat.txt", "x x")
        with self.assertRaisesRegex(ValueError, "EDIT_TEXT_AMBIGUOUS"):
            self.files.edit("repeat.txt", "x", "y")
        self.files.edit("repeat.txt", "x", "y", replace_all=True)
        self.assertEqual(self.files.read("repeat.txt"), "y y")

    def test_read_grant_cannot_write_or_escape_to_sibling(self):
        source = self.external / "source.txt"
        source.write_text("selected data", encoding="utf-8")
        other = self.external / "other.txt"
        other.write_text("not selected", encoding="utf-8")
        self.files.add_grant({"path": str(source), "access": "read", "directory": False})
        self.assertEqual(self.files.read(str(source)), "selected data")
        for call in (lambda: self.files.write(str(source), "bad", overwrite=True),
                     lambda: self.files.read(str(other))):
            with self.assertRaisesRegex(ValueError, "OUTSIDE_WORKSPACE"):
                call()

    def test_dynamic_write_grant_is_shared_by_harness_and_verifier(self):
        harness = TaskHarness(self.root, files=self.files)
        self.files.add_grant({"path": str(self.external), "access": "write", "directory": True})
        path = str(self.external / "result.txt")
        args = {"path": path, "content": "external output"}
        result = self.files.write(**args)
        harness.record_success("write_file", args, result)
        assessment = harness.assess()
        self.assertTrue(assessment["passed"], assessment)
        self.assertEqual(assessment["artifacts"][0]["sha256"], json.loads(result)["sha256"])

    def test_granted_csv_source_is_used_by_analysis_and_source_verification(self):
        source = self.external / "source.csv"
        source.write_text("group,amount\na,10.10\nb,20.20\n", encoding="utf-8")
        self.files.add_grant({"path": str(source), "access": "read", "directory": False})
        data = DataTools(self.root, files=self.files)
        harness = TaskHarness(self.root, files=self.files)
        args = {"path": str(source), "value_column": "amount", "group_by": "group"}
        analysis = data.analyze_csv(**args)
        harness.record_success("analyze_csv", args, analysis)
        value = json.loads(analysis)
        summary = {key: value[key] for key in ("category_totals", "total", "record_count")}
        write_args = {"path": "summary.json", "content": json.dumps(summary)}
        harness.record_success("write_file", write_args, self.files.write(**write_args))
        report = json.loads(harness.verify_artifact("summary.json", kind="csv_summary",
            source_path=str(source), value_column="amount", group_by="group"))
        self.assertTrue(report["passed"], report)

    def test_explicit_empty_output_is_verified_without_allowing_binary_text(self):
        harness = TaskHarness(self.root, files=self.files)
        args = {"path": ".gitkeep", "content": ""}
        harness.record_success("write_file", args, self.files.write(**args))
        self.assertEqual(harness.finalize("created")[1], "completed")
        self.assertFalse(ArtifactVerifier(self.root).verify(".gitkeep")["passed"])
        self.assertTrue(ArtifactVerifier(self.root).verify(".gitkeep", expected_bytes=0)["passed"])
        self.files.write("binary.txt", "a\0b")
        self.assertFalse(ArtifactVerifier(self.root).verify("binary.txt", expected_bytes=3)["passed"])


class RecoverableHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-recovery-")
        self.root = Path(self.temporary.name)
        self.harness = TaskHarness(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_exploratory_bad_path_does_not_block_real_alternative(self):
        self.harness.record_failure("read_file", {"path": "typo.txt"})
        (self.root / "correct.txt").write_text("real input", encoding="utf-8")
        self.harness.record_success("read_file", {"path": "correct.txt"}, self.harness.files.read("correct.txt"))
        self.assertEqual(self.harness.finalize("read actual input")[1], "completed")
        self.assertEqual(self.harness.failure_events[0]["category"], "exploratory")

    def test_explicit_required_input_remains_blocked_after_unrelated_progress(self):
        self.harness.record_failure("read_file", {"path": "required.txt"}, classification="required")
        self.harness.record_success("system_info", {}, "{}")
        self.assertEqual(self.harness.finalize("not enough")[1], "needs_user")
        (self.root / "required.txt").write_text("supplied", encoding="utf-8")
        self.harness.record_success("read_file", {"path": "required.txt"}, "supplied")
        self.assertEqual(self.harness.finalize("now complete")[1], "completed")

    def test_uncertain_effect_is_not_cleared_by_a_repeated_success_claim(self):
        self.harness.record_failure("desktop_act", {"ref": "submit", "action": "invoke"})
        self.harness.record_success("desktop_act", {"ref": "submit", "action": "invoke"}, '{"sent":true}')
        self.assertEqual(self.harness.finalize("claim")[1], "needs_user")

    def test_failed_calculation_cannot_finish_without_a_successful_replacement(self):
        self.harness.record_failure("calculate", {"expression": "1 / 0"})
        self.assertEqual(self.harness.finalize("claim")[1], "needs_user")
        self.harness.record_success("calculate", {"expression": "1 / 2"}, '{"result":"0.5"}')
        self.assertEqual(self.harness.finalize("calculated")[1], "completed")

    def test_only_consecutive_identical_failures_trigger_stall(self):
        for _ in range(6):
            self.harness.record_failure("read_file", {"path": "optional.txt"})
            self.harness.record_success("system_info", {}, "{}")
        self.assertFalse(self.harness.stalled)
        for _ in range(4):
            self.harness.record_failure("read_file", {"path": "optional.txt"})
        self.assertTrue(self.harness.stalled)
        self.harness.record_failure("read_file", {"path": "different.txt"})
        self.assertFalse(self.harness.stalled)

    def test_replan_keeps_step_identity_and_removed_work_auditable(self):
        self.harness.update_plan([{"id": "source", "content": "try candidate A", "status": "in_progress"},
                                  {"id": "verify", "content": "verify output", "status": "pending"}])
        result = json.loads(self.harness.update_plan([
            {"id": "source", "content": "use actual source B", "status": "completed"},
            {"id": "save", "content": "save output", "status": "pending"}], reason="candidate A is unavailable"))
        self.assertEqual(self.harness.plan[0]["id"], "source")
        self.assertEqual(result["superseded"][0]["id"], "verify")
        self.assertEqual(result["superseded"][0]["status"], "superseded")
        self.assertEqual(self.harness.finalize("too early")[1], "needs_user")

    def test_cancelled_steps_require_a_reason_and_do_not_count_as_pending(self):
        with self.assertRaises(HarnessError):
            self.harness.update_plan([{"content": "obsolete", "status": "cancelled"}])
        self.harness.update_plan([{"content": "obsolete", "status": "cancelled", "reason": "superseded route"}])
        self.assertEqual(self.harness.finalize("done")[1], "completed")


if __name__ == "__main__":
    unittest.main()
