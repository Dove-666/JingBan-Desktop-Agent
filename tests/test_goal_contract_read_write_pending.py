"""Same-path read/write regressions repaired after the preserved first benchmark."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"))
from goal_contract import infer_file_requirements


class GoalContractSamePathPreparedTests(unittest.TestCase):
    def test_read_then_write_same_path_preserves_exact_write_requirement(self):
        items = infer_file_requirements('Read report.txt, then write report.txt containing exactly "HELLO".')
        self.assertEqual(items, [{"kind": "artifact", "path": "report.txt", "format": "auto", "exact_text": "HELLO"}])

    def test_chinese_read_then_write_same_path_preserves_exact_write_requirement(self):
        items = infer_file_requirements('读取report.txt，再写入report.txt，内容为“你好”')
        self.assertEqual(items, [{"kind": "artifact", "path": "report.txt", "format": "auto", "exact_text": "你好"}])


if __name__ == "__main__":
    unittest.main()
