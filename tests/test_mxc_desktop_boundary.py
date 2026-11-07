"""Source/gating checks only; explicit tool flag owns the one real canary run."""
import ast
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from tools import audit_mxc_desktop_boundary as subject


class MxcDesktopCanaryTests(unittest.TestCase):
    def test_without_explicit_flag_no_window_native_or_file_is_touched(self):
        stdout=io.StringIO()
        with patch('sys.argv',['owned-canary']),patch.object(subject.C,'WinDLL',side_effect=AssertionError('no window')), \
                patch.object(subject.tempfile,'TemporaryDirectory',side_effect=AssertionError('no files')),redirect_stdout(stdout):
            self.assertEqual(0,subject.main())
        self.assertEqual('FIXED_DESKTOP_CANARY_NOT_REQUESTED',json.loads(stdout.getvalue())['code'])

    def test_fixed_child_code_contains_no_enumeration_input_or_clipboard_data_apis(self):
        attributes={node.attr for node in ast.walk(ast.parse(subject.FIXED_CODE)) if isinstance(node,ast.Attribute)}
        forbidden={'FindWindowW','FindWindowExW','EnumWindows','EnumChildWindows','SendInput','mouse_event','keybd_event',
            'GetClipboardData','SetClipboardData','EmptyClipboard','GetForegroundWindow'}
        self.assertFalse(attributes & forbidden)
        self.assertTrue({'GetWindowThreadProcessId','GetWindowTextW','SendMessageTimeoutW','OpenClipboard','CloseClipboard'}<=attributes)
        self.assertIn("input['hwnd']",subject.FIXED_CODE)
        self.assertIn("if out['target_identity_visible']",subject.FIXED_CODE)

    def test_each_observed_owned_access_blocks_bootstrap_without_claiming_clipboard_content(self):
        for value,changed in (({'owned_title_hash_match':True},False),({'clipboard_opened':True},False),({},True)):
            result=subject._classification(value,changed)
            self.assertTrue(result['access_detected']);self.assertTrue(result['bootstrap_blocked'])
        possible=subject._classification({'window_set_returned':True},False)
        self.assertTrue(possible['bootstrap_blocked']);self.assertTrue(possible['possible_window_write'])
        self.assertFalse(subject._classification({},False)['bootstrap_blocked'])


if __name__=='__main__':unittest.main()
