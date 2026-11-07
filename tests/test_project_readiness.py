"""Capability metadata follows captured host policy; no SDK/native/model runs."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import execution_runtime as execution
from desktop_pet.agents import AgentService
from desktop_pet.agents.checkpoints import scope_fingerprint
from desktop_pet.agents.settings import AGENT_WORKSPACE_ROOT, AgentSettings
from agent_backend.nanobot import project_runtime


class ProjectReadinessTests(unittest.TestCase):
    def setUp(self):
        AGENT_WORKSPACE_ROOT.mkdir(exist_ok=True)
        self.owner = tempfile.TemporaryDirectory(prefix='project-readiness-', dir=AGENT_WORKSPACE_ROOT)
        self.addCleanup(self.owner.cleanup)
        self.root = Path(self.owner.name)
        self.workspace = self.root/'workspace'; self.workspace.mkdir()
        self.private = self.root/'private'; self.private.mkdir()
        self.configuration = self.private/'runtime.json'
        for name, value in (('_ROOT', self.private), ('_CONFIG', self.configuration)):
            mocked = patch.object(execution, name, value); mocked.start(); self.addCleanup(mocked.stop)
        browser = patch('agent_backend.nanobot.browser_session.find_browser', return_value=None)
        browser.start(); self.addCleanup(browser.stop)
        self.service = AgentService(); self.addCleanup(self.service.shutdown)
        self.settings = replace(AgentSettings(), workspace=str(self.workspace), enabled=True, tools_enabled=True)
        self.key = 'SYNTHETIC_READINESS_KEY'
        self.context = {'api': {'base_url': 'https://synthetic.invalid/v1', 'model': 'readiness-only', 'reasoning_effort': 'medium'},
            'messages': [], 'conversation_id': str(uuid4())}

    def write(self, enabled=True):
        self.configuration.write_text(json.dumps({'engine': 'mxc', 'enabled': enabled}), encoding='utf-8')
        return execution.capture_execution_runtime()

    def status(self, *, settings=None, context=None):
        with patch('subprocess.Popen', side_effect=AssertionError('readiness cannot launch')):
            return self.service.readiness(settings or self.settings, self.key,
                self.context if context is None else context)

    @staticmethod
    def compute(status):
        return next(item for item in status['capabilities'] if item['name'] == 'compute_project_artifact')

    def test_missing_or_explicitly_disabled_policy_does_not_advertise_compute(self):
        for mode in ('missing', 'disabled'):
            with self.subTest(mode=mode):
                if mode == 'disabled': self.write(False)
                result = self.status()
                self.assertTrue(result['available']); self.assertIn('read_file', result['tools'])
                self.assertNotIn('compute_project_artifact', result['tools'])
                self.assertFalse(self.compute(result)['available'])
                self.assertEqual('execution_runtime_disabled', self.compute(result)['reason_code'])
                self.assertEqual(mode != 'missing', self.configuration.exists())

    def test_static_permission_manifest_needs_host_scope_without_reading_config(self):
        self.write(True)
        with patch.object(execution, 'capture_execution_runtime', side_effect=AssertionError('static manifest cannot read host config')):
            manifest = {item['name']: item for item in self.service.capabilities(self.settings)}
        self.assertFalse(manifest['compute_project_artifact']['available'])
        self.assertEqual('needs_host_scope', manifest['compute_project_artifact']['reason_code'])

    def test_enabled_valid_snapshot_and_canonical_conversation_advertise_only_metadata(self):
        snapshot = self.write(True); before = self.configuration.read_bytes()
        result = self.status(context={**self.context, 'execution_runtime_snapshot': snapshot})
        self.assertTrue(result['available']); self.assertIn('compute_project_artifact', result['tools'])
        self.assertTrue(self.compute(result)['available']); self.assertEqual('', self.compute(result)['reason_code'])
        self.assertEqual(before, self.configuration.read_bytes())
        self.assertEqual([], list(self.workspace.iterdir()))

    def test_enabled_policy_without_conversation_keeps_regular_tools_only(self):
        self.write(True)
        context = {k: v for k, v in self.context.items() if k != 'conversation_id'}
        result = self.status(context=context)
        self.assertTrue(result['available']); self.assertIn('read_file', result['tools'])
        self.assertNotIn('compute_project_artifact', result['tools'])
        self.assertEqual('conversation_required', self.compute(result)['reason_code'])

    def test_malformed_policy_or_dictionary_snapshot_never_advertises_tools_or_leaks_values(self):
        secret = 'SYNTHETIC_PRIVATE_CONFIG_VALUE'
        for context in (self.context, {**self.context, 'execution_runtime_snapshot': {'enabled': True, 'secret': secret}}):
            with self.subTest(supplied_snapshot='execution_runtime_snapshot' in context):
                self.configuration.write_text(json.dumps({'engine': 'mxc', 'enabled': True, 'secret': secret}), encoding='utf-8')
                result = self.status(context=context)
                self.assertFalse(result['available']); self.assertEqual([], result['tools'])
                self.assertEqual('execution_runtime_invalid', result['reason_code'])
                self.assertNotIn(secret, json.dumps(result))

    def test_tools_and_platform_still_gate_enabled_policy(self):
        self.write(True)
        result = self.status(settings=replace(self.settings, tools_enabled=False))
        self.assertNotIn('compute_project_artifact', result['tools'])
        self.assertEqual('kind_disabled', self.compute(result)['reason_code'])
        with patch('desktop_pet.agents.service.sys.platform', 'linux'):
            result = self.status()
        self.assertNotIn('compute_project_artifact', result['tools'])
        self.assertEqual('gui_unsupported_platform', self.compute(result)['reason_code'])

    def test_stale_but_genuine_snapshot_keeps_the_existing_whole_dispatch_barrier(self):
        snapshot = self.write(True)
        self.write(False)
        result = self.status(context={**self.context, 'execution_runtime_snapshot': snapshot})
        self.assertFalse(result['available']); self.assertEqual([], result['tools'])
        self.assertEqual('execution_runtime_changed', result['reason_code'])

    def test_only_current_conversation_scope_grants_can_remove_compute(self):
        self.write(True)
        from desktop_pet.agents.contracts import AgentRequest
        config, _, secrets = self.service._context(self.context, self.key, AgentRequest('readiness', 'agent'), self.settings)
        scope = scope_fingerprint(self.settings, config, secrets=secrets, generation='')
        protected_source = Path(project_runtime.__file__).resolve().parent
        grant = {'path': str(protected_source), 'access': 'read', 'directory': True}
        self.service._file_grants[(self.context['conversation_id'], 'old-scope')] = [grant]
        self.assertIn('compute_project_artifact', self.status()['tools'])
        self.service._file_grants[(self.context['conversation_id'], scope)] = [grant]
        result = self.status()
        self.assertTrue(result['available']); self.assertIn('read_file', result['tools'])
        self.assertNotIn('compute_project_artifact', result['tools'])
        self.assertEqual('execution_scope_conflict', self.compute(result)['reason_code'])

    def test_shared_scope_rule_excludes_runtime_and_containing_or_nested_paths(self):
        self.write(True)
        for target in (self.private, self.private.parent, self.configuration,
                       project_runtime.mxc.CODEX.parent, project_runtime.mxc.PYTHON_ROOT,
                       Path(project_runtime.__file__).resolve().parent):
            with self.subTest(target=str(target)):
                self.assertTrue(project_runtime.project_scope_conflicts([target],
                    host_private_root=self.private, host_configuration=self.configuration))
        self.assertFalse(project_runtime.project_scope_conflicts([self.workspace],
            host_private_root=self.private, host_configuration=self.configuration))


if __name__ == '__main__':
    unittest.main()
