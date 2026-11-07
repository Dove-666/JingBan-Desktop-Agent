"""Declared compute availability must match host scope; never enable or execute it."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentService, AgentRequest
from desktop_pet.agents import execution_runtime as host
from desktop_pet.agents import office_runtime as office
from desktop_pet.agents import service as service_module
from desktop_pet.agents.settings import AgentSettings, AGENT_WORKSPACE_ROOT, shared_api_settings
from desktop_pet.agents.checkpoints import scope_fingerprint
from agent_backend.nanobot import mxc_execution


class ProjectReadinessIndependentTests(unittest.TestCase):
    def setUp(self):
        AGENT_WORKSPACE_ROOT.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='project-readiness-independent-', dir=AGENT_WORKSPACE_ROOT)
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        workspace = root/'workspace'; workspace.mkdir()
        private = root/'private'; private.mkdir()
        self.configuration = private/'runtime.json'
        self.settings = replace(AgentSettings(), workspace=str(workspace))
        self.context = {'api':{'base_url':'http://127.0.0.1:9/v1','model':'fixed-no-request'},
                        'messages':[], 'conversation_id':str(uuid4())}
        # Production normalizes the one global API before grant fingerprints.
        self.settings = shared_api_settings(self.settings, self.context['api'])
        self.service = AgentService()
        self.addCleanup(self.service.shutdown)
        self.patches = (
            patch.object(host, '_ROOT', private), patch.object(host, '_CONFIG', self.configuration),
            patch.object(office, '_ROOT', root/'office-private'),
            patch.object(office, '_CONFIG', root/'office-private'/'runtime.json'),
        )
        for boundary in self.patches:
            boundary.start(); self.addCleanup(boundary.stop)

    def status(self, *, settings=None, context=None):
        with patch('subprocess.Popen', side_effect=AssertionError('Metadata cannot launch a process')), \
             patch('agent_backend.nanobot.project_runtime.ProjectRuntime.__init__',
                   side_effect=AssertionError('Metadata cannot mint an execution owner')):
            return self.service.readiness(settings or self.settings, 'FIXED_SYNTHETIC_NO_NETWORK_KEY',
                                          self.context if context is None else context)

    def assert_ordinary_tools(self, status):
        self.assertTrue(status['available'])
        self.assertTrue({'read_file', 'write_file', 'calculate'} <= set(status['tools']))

    def test_missing_and_explicitly_off_compute_do_not_disable_normal_tools(self):
        self.assertFalse(self.configuration.exists())
        missing = self.status()
        self.assert_ordinary_tools(missing)
        self.assertNotIn('compute_project_artifact', missing['tools'])
        self.assertFalse(self.configuration.exists(), 'Readiness must never create opt-in configuration')
        self.configuration.write_text('{"engine":"mxc","enabled":false}', encoding='utf-8')
        original = self.configuration.read_bytes()
        off = self.status()
        self.assert_ordinary_tools(off)
        self.assertNotIn('compute_project_artifact', off['tools'])
        self.assertEqual(original, self.configuration.read_bytes())

    def test_enabled_owned_fixture_is_declared_only_with_a_canonical_conversation(self):
        self.configuration.write_text('{"engine":"mxc","enabled":true}', encoding='utf-8')
        with patch.object(service_module.sys, 'platform', 'win32'):
            status = self.status()
        self.assert_ordinary_tools(status)
        self.assertIn('compute_project_artifact', status['tools'])
        context = {key:value for key,value in self.context.items() if key != 'conversation_id'}
        legacy = self.status(context=context)
        self.assert_ordinary_tools(legacy)
        self.assertNotIn('compute_project_artifact', legacy['tools'])
        self.assertEqual('{"engine":"mxc","enabled":true}', self.configuration.read_text(encoding='utf-8'))

    def test_nonwindows_and_disabled_tool_permission_cannot_declare_compute(self):
        self.configuration.write_text('{"engine":"mxc","enabled":true}', encoding='utf-8')
        with patch.object(service_module.sys, 'platform', 'linux'):
            status = self.status()
        self.assert_ordinary_tools(status)
        self.assertNotIn('compute_project_artifact', status['tools'])
        status = self.status(settings=replace(self.settings, tools_enabled=False, computer_enabled=True))
        self.assertTrue(status['available'])
        self.assertNotIn('compute_project_artifact', status['tools'])
        self.assertNotIn('read_file', status['tools'])
        self.assertIn('discover_applications', status['tools'])

    def test_static_manifest_cannot_grant_host_compute_or_read_a_runtime_file(self):
        self.configuration.write_text('{"engine":"mxc","enabled":true}', encoding='utf-8')
        with patch.object(host, 'capture_execution_runtime', side_effect=AssertionError('No host lookup from static metadata')):
            manifest = {row['name']:row for row in AgentService.capabilities(self.settings)}
        self.assertTrue(manifest['read_file']['available'])
        self.assertFalse(manifest['compute_project_artifact']['available'])

    def test_forged_snapshot_and_invalid_conversation_remain_rejected(self):
        for extra in ({'execution_runtime_snapshot':{'enabled':True}},
                      {'conversation_id':'not-a-canonical-uuid'},
                      {'conversation_id':str(uuid4()),'conversation_generation':'invalid'}):
            with self.subTest(extra=extra):
                status = self.status(context={**self.context, **extra})
                self.assertFalse(status['available'])
                self.assertEqual([], status['tools'])
        self.assertFalse(self.configuration.exists())

    def grant_scope(self):
        configuration, _, secrets = self.service._context(
            self.context, 'FIXED_SYNTHETIC_NO_NETWORK_KEY', AgentRequest('readiness', 'agent'), self.settings)
        return scope_fingerprint(self.settings, configuration, secrets=secrets,
                                 generation=self.context.get('conversation_generation', ''))

    def test_active_conflicting_runtime_grant_disables_only_compute(self):
        self.configuration.write_text('{"engine":"mxc","enabled":true}', encoding='utf-8')
        scope = self.grant_scope()
        self.service._file_grants[(self.context['conversation_id'], scope)] = [
            {'path':str(mxc_execution.PYTHON_ROOT), 'access':'read', 'directory':True}]
        status = self.status()
        self.assert_ordinary_tools(status)
        self.assertNotIn('compute_project_artifact', status['tools'])

    def test_foreign_conversation_and_stale_scope_grants_do_not_hide_compute(self):
        self.configuration.write_text('{"engine":"mxc","enabled":true}', encoding='utf-8')
        scope = self.grant_scope()
        conflicting = [{'path':str(mxc_execution.PYTHON_ROOT), 'access':'read', 'directory':True}]
        self.service._file_grants[(str(uuid4()), scope)] = conflicting
        self.service._file_grants[(self.context['conversation_id'], 'f'*64)] = conflicting
        status = self.status()
        self.assert_ordinary_tools(status)
        self.assertIn('compute_project_artifact', status['tools'])


if __name__ == '__main__':
    unittest.main()
