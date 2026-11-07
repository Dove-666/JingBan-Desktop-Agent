"""Top-level file obligations must not be invented from archive contents."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from goal_contract import GoalContract, infer_file_requirements
from goal_evidence import GoalEvidence
from safe_files import WorkspaceFiles


def paths(task):
    return [(item["kind"], item["path"]) for item in infer_file_requirements(task)]


class GoalContractContainerMemberTests(unittest.TestCase):
    def test_original_delivered_zip_request_requires_the_container_only(self):
        task = ("Create delivered.zip containing source.txt and result.json with the exact sum 46. "
                "Verify the actual ZIP before completion.")
        contract = GoalContract.from_task(task)
        self.assertEqual([("artifact", "delivered.zip")],
                         [(item["kind"], item["path"]) for item in contract.requirements])
        self.assertTrue(contract.require_judge)

    def test_container_member_order_and_intro_words_do_not_add_workspace_outputs(self):
        for intro in ("containing", "with", "including", "that includes", "containing files"):
            for members in ("source.txt and result.json", "result.json and source.txt"):
                task = f"Create delivered.zip {intro} {members}"
                with self.subTest(task=task):
                    self.assertEqual([("artifact", "delivered.zip")], paths(task))
        for task in ("创建 delivered.zip，包含 source.txt 和 result.json",
                     "生成 delivered.zip，其中包括文件 result.json 和 source.txt"):
            with self.subTest(task=task):
                self.assertEqual([("artifact", "delivered.zip")], paths(task))

    def test_direct_zip_outputs_keep_a_real_file_guard(self):
        for task in ("Create delivered.zip", 'Create "delivered.zip"', "Write delivered.zip",
                     "Save a file named delivered.zip", "请创建delivered.zip", "生成 `delivered.zip`"):
            with self.subTest(task=task):
                self.assertEqual([("artifact", "delivered.zip")], paths(task))

    def test_actual_top_level_and_lists_still_require_all_named_files(self):
        for task, names in (("Create a.txt and b.txt", ["a.txt", "b.txt"]),
                            ("Create a.txt and b.txt.", ["a.txt", "b.txt"]),
                            ("Create a.txt, b.txt, and c.txt", ["a.txt", "b.txt", "c.txt"]),
                            ("Create a.txt,b.txt,and c.txt.", ["a.txt", "b.txt", "c.txt"]),
                            ('Create "a.txt" and the file "b.txt"', ["a.txt", "b.txt"]),
                            ("Create delivered.zip and note.txt", ["delivered.zip", "note.txt"]),
                            ("创建 a.txt 和 b.txt", ["a.txt", "b.txt"]),
                            ("创建 a.txt、b.txt", ["a.txt", "b.txt"])):
            with self.subTest(task=task):
                self.assertEqual([("artifact", name) for name in names], paths(task))
        self.assertEqual([("read_file", "a.txt"), ("read_file", "b.txt")], paths("Read a.txt and b.txt"))

    def test_input_references_are_not_new_outputs(self):
        for task in ("Create output.json from source.txt and input.csv",
                     "Create output.json using source.txt and input.csv",
                     "Create output.json based on source.txt and input.csv",
                     "Create output.json as a summary of the file source.txt and input.csv"):
            with self.subTest(task=task):
                self.assertEqual([("artifact", "output.json")], paths(task))
        self.assertEqual([("read_file", "source.txt"), ("artifact", "output.json")],
                         paths("Read source.txt and create output.json from input.csv"))

    def test_quoted_body_text_does_not_become_a_subsequent_filename(self):
        for task in ('Create report.txt containing exactly "source.txt and result.json"',
                     'Create report.txt with text "and result.json"',
                     "Create report.txt with 'file source.txt and result.json' as its contents",
                     'Create delivered.zip containing "source.txt" and "result.json"'):
            with self.subTest(task=task):
                name = "delivered.zip" if "delivered.zip" in task else "report.txt"
                self.assertEqual([("artifact", name)], paths(task))

    def test_sentence_periods_do_not_accept_partial_extension_chains(self):
        for task, expected in (("Create report.txt.json.", ["report.txt.json"]),
                               ("Create report.txt.unknown", []),
                               ("Create v1.2.3.", []), ("Read example.com.", []),
                               ("Read response.json().", [])):
            with self.subTest(task=task):
                self.assertEqual(expected, [item["path"] for item in infer_file_requirements(task)])

    def test_explicit_zip_format_validates_actual_container_bytes(self):
        with tempfile.TemporaryDirectory(prefix="aura-goal-zip-format-") as temporary:
            root = Path(temporary)
            files, evidence = WorkspaceFiles(root), GoalEvidence()
            contract = GoalContract("Deliver a valid ZIP", require_judge=False,
                                    requirements=[{"path": "delivered.zip", "format": "zip"}])
            snapshot = contract.snapshot(files, evidence, answer="Done")
            self.assertFalse(contract.assess(snapshot)["passed"])
            (root / "delivered.zip").write_bytes(b"plain text is not a ZIP")
            self.assertFalse(contract.assess(contract.snapshot(files, evidence, answer="Done"))["passed"])
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("result.json", '{"total":46}')
            (root / "delivered.zip").write_bytes(stream.getvalue())
            self.assertTrue(contract.assess(contract.snapshot(files, evidence, answer="Done"))["passed"])

    def test_missing_inferred_zip_and_second_top_level_file_still_block_completion(self):
        with tempfile.TemporaryDirectory(prefix="aura-goal-missing-outputs-") as temporary:
            root = Path(temporary)
            files, evidence = WorkspaceFiles(root), GoalEvidence()
            for task in ("Create delivered.zip containing source.txt and result.json",
                         "Create a.txt and b.txt"):
                with self.subTest(task=task):
                    (root / "a.txt").write_text("A", encoding="utf-8")
                    contract = GoalContract.from_task(task)
                    snapshot = contract.snapshot(files, evidence, answer="Done")
                    verdict = {"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
                               "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
                               "status": "satisfied", "evidence_ids": snapshot["evidence_ids"]}
                    report = contract.assess(snapshot, verdict)
                    self.assertFalse(report["passed"])
                    self.assertIn("GOAL_REQUIREMENT_FAILED", report["issues"])


if __name__ == "__main__":
    unittest.main()
