"""Natural Chinese filenames/lists must not create invented output paths."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend/nanobot"))
from goal_contract import GoalContract, infer_file_requirements
from goal_evidence import GoalEvidence
from safe_files import WorkspaceFiles


def paths(task): return [row["path"] for row in infer_file_requirements(task)]


class ChineseFileOperandTests(unittest.TestCase):
    def test_adjacent_chinese_lists_keep_each_real_output(self):
        for join in ("和", "与", "及", "以及"):
            with self.subTest(join=join):
                self.assertEqual(["活动安排.docx", "活动安排.pdf"], paths(f"请生成活动安排.docx{join}活动安排.pdf。"))
        self.assertEqual(["甲.txt", "乙.json", "丙.csv"], paths("请创建甲.txt和乙.json以及丙.csv。"))

    def test_chinese_connections_inside_a_basename_are_preserved(self):
        for name in ("年会和活动安排.docx", "甲与乙的预算.xlsx", "以及后续计划.txt", "一个人的书.docx"):
            with self.subTest(name=name): self.assertEqual([name], paths("创建" + name + "。"))
        self.assertEqual(["甲.txt和乙.txt"], paths('创建"甲.txt和乙.txt"。'))

    def test_adjacent_list_can_mix_bare_and_explicitly_quoted_filenames(self):
        for task in ('创建甲.txt和"乙.txt"。', '创建"甲.txt"和乙.txt。', '创建“甲.txt”和“乙.txt”。'):
            with self.subTest(task=task): self.assertEqual(["甲.txt", "乙.txt"], paths(task))

    def test_explicit_format_file_intros_do_not_become_part_of_the_name(self):
        for task in ("请生成一个Word文件活动安排.docx，同时输出PDF文件活动安排.pdf。",
                     "请创建一份PDF文档通知.pdf。", "创建Word文档总结.docx和Excel表格预算.xlsx。"):
            expected = ["通知.pdf"] if "通知.pdf" in task else ["总结.docx", "预算.xlsx"] if "总结.docx" in task else ["活动安排.docx", "活动安排.pdf"]
            with self.subTest(task=task): self.assertEqual(expected, paths(task))
        self.assertEqual(["Word文件.docx"], paths("创建Word文件.docx。"))

    def test_archive_members_input_names_and_quoted_body_are_not_outputs(self):
        for task in ("创建结果.zip，里面包含甲.txt和乙.json。", "创建结果.zip，其中包括甲.txt及乙.json。",
                     "根据甲.txt和乙.json，创建结果.zip。"):
            with self.subTest(task=task): self.assertEqual(["结果.zip"], paths(task))
        rows = infer_file_requirements("创建甲.txt，内容为“请保留乙.json和丙.csv”。")
        self.assertEqual(["甲.txt"], [row["path"] for row in rows])
        self.assertEqual("请保留乙.json和丙.csv", rows[0]["exact_text"])

    def test_console_output_is_not_a_new_file_creation_verb(self):
        for task in ("代码输出 hello.txt 是什么意思？", "解释一下程序输出PDF文件名的原因", "输出结果是 alpha.json 和 beta.json"):
            with self.subTest(task=task): self.assertEqual([], paths(task))

    def test_actual_two_files_can_pass_but_missing_second_still_blocks(self):
        with tempfile.TemporaryDirectory(prefix="goal-chinese-two-files-") as temporary:
            root = Path(temporary); (root / "甲.txt").write_text("甲", encoding="utf-8")
            contract = GoalContract.from_task("创建甲.txt和乙.txt。")
            def assess():
                snapshot = contract.snapshot(WorkspaceFiles(root), GoalEvidence(), answer="已创建两份文件")
                verdict = {"schema":"aura.goal_verdict.v1", "goal_id":snapshot["goal_id"],
                    "evidence_id":snapshot["evidence_id"], "answer_sha256":snapshot["answer_sha256"],
                    "status":"satisfied", "evidence_ids":snapshot["evidence_ids"]}
                return contract.assess(snapshot, verdict)
            self.assertFalse(assess()["passed"])
            (root / "乙.txt").write_text("乙", encoding="utf-8")
            self.assertTrue(assess()["passed"])


if __name__ == "__main__": unittest.main()
