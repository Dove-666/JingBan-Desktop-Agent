"""Ordinary strings/model dictionaries cannot mint project write witnesses."""
import json
import tempfile
from pathlib import Path
import unittest

from agent_backend.nanobot.tests import test_worker as fixture
from task_harness import TaskHarness, HarnessError
from goal_evidence import GoalEvidence
from safe_files import WorkspaceFiles


class ProjectEvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='aura-project-evidence-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.value={'schema':'aura.project_artifact.v1','written':'fake.txt','sha256':'a'*64,'bytes':42,
            'artifact':{'path':'fake.txt','sha256':'a'*64,'bytes':42},'task_verified':False}

    def test_plain_model_shaped_json_cannot_record_a_write_or_original_goal_observation(self):
        for value in (self.value,json.dumps(self.value)):
            harness=TaskHarness(self.root,files=WorkspaceFiles(self.root))
            with self.assertRaisesRegex(HarnessError,'PROJECT_PUBLICATION_RECEIPT_REQUIRED'):
                harness.record_success('compute_project_artifact',{'output':{'target':'fake.txt'}},value)
            self.assertEqual(set(),harness.writes)
            self.assertEqual([],harness.goal_evidence.snapshot()['observations'])
            self.assertFalse((self.root/'fake.txt').exists())

    def test_independent_evidence_projection_rejects_plain_result_without_effect_witness(self):
        for value in (self.value,json.dumps(self.value),True):
            evidence=GoalEvidence()
            with self.assertRaisesRegex(ValueError,'PROJECT_PUBLICATION_RECEIPT_REQUIRED'):
                evidence.observe('compute_project_artifact',{},value)
            self.assertFalse(evidence.has_effects)
            self.assertEqual([],evidence.snapshot()['observations'])


if __name__=='__main__':unittest.main()
