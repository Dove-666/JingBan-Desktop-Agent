"""Installer driver argument/dispatch checks; no installation, UI or process launch."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


@unittest.skipUnless(sys.platform == 'win32', 'Windows acceptance driver')
class InstallerAcceptanceModesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'tools/qa_release/accept_installer.py'
        spec = importlib.util.spec_from_file_location('installer_acceptance_fixture', source)
        cls.driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.driver)

    def test_default_keeps_interactive_mode_and_does_not_load_driver_during_parse(self):
        with patch.object(self.driver, 'interactive_driver') as load:
            args = self.driver.parse_arguments(['installer.exe', 'payload'])
        self.assertFalse(args.non_interactive)
        self.assertFalse(args.verify_dsh_runtime)
        self.assertFalse(args.verify_private_literals)
        load.assert_not_called()

    def test_non_interactive_and_optional_audits_are_explicit(self):
        args = self.driver.parse_arguments(['installer.exe', 'payload', '--non-interactive',
            '--verify-dsh-runtime', '--verify-private-literals'])
        self.assertTrue(args.non_interactive)
        self.assertTrue(args.verify_dsh_runtime)
        self.assertTrue(args.verify_private_literals)

    def test_help_probe_launches_help_only_and_checks_owned_profile_unchanged(self):
        output = MagicMock()
        with patch.object(self.driver, 'profile_digests', return_value={'marker': 'same'}) as digests, \
                patch.object(self.driver.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'', stderr=b'')) as run:
            result = self.driver.help_probe(Path('owned.exe'), Path('owned-work'), Path('owned-profile'), {}, output)
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ['owned.exe', '--help'])
        self.assertEqual(run.call_args.kwargs['creationflags'], self.driver.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(digests.call_count, 2)

    def test_background_audits_disable_input_and_keep_original_audit_environment(self):
        args = SimpleNamespace(verify_dsh_runtime=True, verify_private_literals=True)
        report = {'checks': {}}
        original = {'PATH': 'synthetic-path', 'JINGBAN_PROFILE_DIR': 'synthetic-existing-audit-profile'}
        with patch.dict(os.environ, original, clear=True), \
                patch.object(self.driver.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.driver.optional_background_checks(args, Path('owned-install'), Path('owned-work'), Path('reports'), report)
        self.assertEqual(run.call_count, 2)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn('--skip-native-input', commands[0])
        self.assertNotIn('--fixture', commands[0])
        self.assertIn('verify_private_literals.py', commands[1][1])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['env'], original)
        self.assertEqual(set(report['checks']), {'installed_dsh_runtime', 'installed_private_literals'})


if __name__ == '__main__':
    unittest.main()
