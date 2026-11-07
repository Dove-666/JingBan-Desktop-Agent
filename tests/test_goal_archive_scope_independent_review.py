"""Independent archive/member vs workspace-goal regressions; no model or native IO."""
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent_backend' / 'nanobot'))
from goal_contract import GoalContract, infer_file_requirements
from goal_evidence import GoalEvidence
from safe_files import WorkspaceFiles


class ArchiveGoalScopeIndependentTests(unittest.TestCase):
    def paths(self, task):
        return [(item['kind'], item['path']) for item in infer_file_requirements(task)]

    def test_english_archive_members_do_not_become_workspace_outputs(self):
        for task in (
            'Create delivered.zip containing source.txt and result.json with the exact sum 46.',
            'Create delivered.zip containing a.txt, b.json and c.csv.',
            'Create delivered.zip with members "source.txt" and "result.json".',
            'Create delivered.zip; include source.txt and result.json inside the ZIP.',
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', 'delivered.zip')], self.paths(task))

    def test_chinese_archive_members_do_not_become_workspace_outputs(self):
        for task in (
            '创建结果.zip，里面包含 source.txt 和 result.json。',
            '创建结果.zip，包含 `source.txt` 和 `result.json`，结果放在压缩包里。',
            '创建一个名为 结果.zip 的文件，其中包括 source.txt、result.json。',
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', '结果.zip')], self.paths(task))

    def test_actual_english_top_level_file_lists_preserve_every_output(self):
        for task, expected in (
            ('Create a.txt and b.json.', ['a.txt', 'b.json']),
            ('Create a.txt, b.json, and c.csv.', ['a.txt', 'b.json', 'c.csv']),
            ('Create a.txt and create b.json.', ['a.txt', 'b.json']),
            ('Create delivered.zip and summary.json.', ['delivered.zip', 'summary.json']),
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', path) for path in expected], self.paths(task))

    def test_actual_chinese_top_level_file_lists_preserve_every_output(self):
        for task, expected in (
            ('创建 a.txt 和 b.json', ['a.txt', 'b.json']),
            ('创建 a.txt、b.json 和 c.csv', ['a.txt', 'b.json', 'c.csv']),
            ('创建 a.txt，并创建 b.json。', ['a.txt', 'b.json']),
            ('创建 delivered.zip 以及 summary.json', ['delivered.zip', 'summary.json']),
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', path) for path in expected], self.paths(task))

    def test_archive_body_does_not_swallow_a_later_explicit_output_command(self):
        for task in (
            'Create delivered.zip containing source.txt and result.json, and create notes.txt.',
            'Create delivered.zip containing source.txt and result.json, then write notes.txt.',
            '创建 delivered.zip，里面包含 source.txt 和 result.json，再创建 notes.txt。',
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', 'delivered.zip'), ('artifact', 'notes.txt')], self.paths(task))

    def test_quoted_content_with_and_and_json_is_only_content(self):
        for task in (
            'Create a.txt containing exactly "hello and b.json".',
            'Create a.txt with text "hello and b.json".',
            'Create a.txt containing exactly `b.json and c.csv`.',
            '创建 a.txt，内容为“b.json 和 c.csv”。',
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', 'a.txt')], self.paths(task))

    def test_quoted_content_does_not_erase_later_explicit_commands(self):
        task = 'Create a.txt containing exactly "b.json and c.csv" and then create d.txt.'
        self.assertEqual([('artifact', 'a.txt'), ('artifact', 'd.txt')], self.paths(task))
        requirements = infer_file_requirements(task)
        self.assertEqual('b.json and c.csv', requirements[0]['exact_text'])

    def test_first_file_quoted_content_does_not_remove_a_second_top_level_file(self):
        for task in (
            'Create a.txt containing exactly "hello" and b.json containing exactly "{}"',
            'Create a.txt containing exactly "hello and ghost.json" and b.json containing exactly "{}"',
            'Create a.txt with text "hello" and b.json',
        ):
            with self.subTest(task=task):
                self.assertEqual([('artifact', 'a.txt'), ('artifact', 'b.json')], self.paths(task))

    def test_exact_content_belongs_to_its_own_file(self):
        requirements = infer_file_requirements('Create a.txt and b.json containing exactly "{}"')
        self.assertEqual(['a.txt', 'b.json'], [item['path'] for item in requirements])
        self.assertNotIn('exact_text', requirements[0])
        self.assertEqual('{}', requirements[1]['exact_text'])
        requirements = infer_file_requirements(
            'Create a.txt containing exactly "hello" and b.json containing exactly "{}"')
        self.assertEqual(['hello', '{}'], [item.get('exact_text') for item in requirements])

    def test_archive_quoted_member_name_still_does_not_authorize_a_file_list(self):
        task = 'Create delivered.zip containing exactly "source.txt" and result.json'
        self.assertEqual([('artifact', 'delivered.zip')], self.paths(task))
        self.assertNotIn('exact_text', infer_file_requirements(task)[0])

    def test_source_input_context_does_not_create_outputs(self):
        for task, expected in (
            ('Create delivered.zip from source.txt and result.json.', [('artifact', 'delivered.zip')]),
            ('Using source.txt and config.json, create delivered.zip.', [('artifact', 'delivered.zip')]),
            ('Read source.txt and create delivered.zip containing source.txt and result.json.',
             [('read_file', 'source.txt'), ('artifact', 'delivered.zip')]),
            ('Read source.txt and config.json, then create delivered.zip.',
             [('read_file', 'source.txt'), ('read_file', 'config.json'), ('artifact', 'delivered.zip')]),
            ('Create delivered.zip and summary.json using source.txt and input.json.',
             [('artifact', 'delivered.zip'), ('artifact', 'summary.json')]),
        ):
            with self.subTest(task=task):
                self.assertEqual(expected, self.paths(task))

    def test_explicit_host_requirements_are_never_removed_as_archive_members(self):
        contract = GoalContract.from_task(
            'Create delivered.zip containing source.txt and result.json.',
            requirements=[{'kind': 'artifact', 'path': 'result.json', 'format': 'json'},
                          {'kind': 'artifact', 'path': 'delivered.zip', 'format': 'zip'}])
        self.assertEqual([('result.json', 'json'), ('delivered.zip', 'zip')],
                         [(item['path'], item['format']) for item in contract.requirements])

    def snapshot(self, task):
        self.directory = tempfile.TemporaryDirectory(prefix='aura-goal-archive-independent-')
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        with zipfile.ZipFile(root/'delivered.zip', 'w') as archive:
            archive.writestr('source.txt', '17 29')
            archive.writestr('result.json', '{"total":46}')
        contract = GoalContract.from_task(task)
        snapshot = contract.snapshot(WorkspaceFiles(root), GoalEvidence(), answer='Fixed QA candidate')
        verdict = {'schema':'aura.goal_verdict.v1', 'goal_id':snapshot['goal_id'],
                   'evidence_id':snapshot['evidence_id'], 'answer_sha256':snapshot['answer_sha256'],
                   'status':'satisfied', 'evidence_ids':snapshot['evidence_ids']}
        return contract, snapshot, verdict

    def test_archive_only_readback_does_not_require_extracting_members(self):
        contract, snapshot, verdict = self.snapshot(
            'Create delivered.zip containing source.txt and result.json with the exact sum 46.')
        self.assertEqual(['delivered.zip'], [item['path'] for item in snapshot['facts']['requirement_checks']])
        self.assertTrue(snapshot['facts']['requirement_checks'][0]['passed'])
        # This optimistic fixture verdict only tests the hard file guard. It
        # does not certify an actual model or the archive's business semantics.
        self.assertTrue(contract.assess(snapshot, verdict)['passed'])
        self.assertEqual(['delivered.zip'], [p.name for p in Path(self.directory.name).iterdir()])

    def test_missing_second_workspace_output_cannot_be_overridden_by_a_judge(self):
        contract, snapshot, verdict = self.snapshot('Create delivered.zip and notes.txt.')
        self.assertEqual(['delivered.zip', 'notes.txt'],
                         [item['path'] for item in snapshot['facts']['requirement_checks']])
        self.assertFalse(contract.assess(snapshot, verdict)['passed'])
        self.assertIn('GOAL_FILE_UNAVAILABLE', snapshot['facts']['requirement_checks'][1]['issues'])


if __name__ == '__main__':
    unittest.main()
