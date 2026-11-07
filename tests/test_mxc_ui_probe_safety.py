"""Probe imports/default invocations never create UI, processes or policy."""
from contextlib import redirect_stdout
import importlib
import io
import json
import unittest
from unittest.mock import patch


class FixedUiProbeSafetyTests(unittest.TestCase):
    def test_no_opt_in_never_invokes_any_probe_or_native_launcher(self):
        names=['audit_mxc_win32k_probe','audit_mxc_python_mitigation',
            'audit_mxc_dotnet_mitigation','audit_mxc_private_station',
            'audit_mxc_native_mitigation','audit_mxc_broker']
        for name in names:
            with self.subTest(name=name):
                module=importlib.import_module('tools.'+name)
                output=io.StringIO()
                with patch.object(module.sys,'argv',[name]), \
                     patch.object(module.native,'run',side_effect=AssertionError('native launch')), \
                     patch('subprocess.Popen',side_effect=AssertionError('process launch')), \
                     patch('ctypes.WinDLL',side_effect=AssertionError('UI/native API')), \
                     redirect_stdout(output):
                    self.assertEqual(0,module.main())
                self.assertTrue(json.loads(output.getvalue())['code'].endswith('_NOT_REQUESTED'))

    def test_original_failed_boundary_report_is_not_a_new_probe_output(self):
        # The new experiments must preserve the original evidence of title
        # access. Check their actual declared report destinations separately.
        for name in ['audit_mxc_win32k_probe','audit_mxc_python_mitigation',
                     'audit_mxc_dotnet_mitigation','audit_mxc_private_station']:
            with self.subTest(name=name):
                module=importlib.import_module('tools.'+name)
                self.assertNotIn('mxc-owned-desktop-fixed-canary.json',
                                 module.Path(module.__file__).read_text(encoding='utf-8'))

    def test_known_boundary_flag_alone_cannot_start_fixed_broker_or_native_work(self):
        from tools import audit_mxc_broker as module
        output=io.StringIO()
        with patch.object(module.native,'run',side_effect=AssertionError('native launch')), \
             patch('subprocess.Popen',side_effect=AssertionError('process launch')), \
             patch('ctypes.WinDLL',side_effect=AssertionError('native API')), \
             redirect_stdout(output):
            self.assertEqual(0,module.main(['--allow-known-boundary-fixed-only']))
        self.assertEqual('FIXED_PRODUCTION_BROKER_NOT_REQUESTED',json.loads(output.getvalue())['code'])


if __name__=='__main__':unittest.main()
