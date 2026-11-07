"""Pure parser/audit controls for fixed canary tooling; no binary runs."""
import io
import json
from pathlib import Path
import struct
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tools import audit_mxc_native_mitigation as probe


def image(dll=b'kernel32.dll',function=b'ExitProcess'):
    # Data-only PE fixture for the import reader. No machine instructions,
    # executable file, mapping, native loader or process creation is involved.
    raw=bytearray(0xa00);raw[:2]=b'MZ';struct.pack_into('<I',raw,60,0x80)
    raw[0x80:0x84]=b'PE\0\0';struct.pack_into('<HH',raw,0x84,0x8664,1)
    struct.pack_into('<H',raw,0x94,240);optional=0x98
    struct.pack_into('<H',raw,optional,0x20b);struct.pack_into('<H',raw,optional+68,3)
    struct.pack_into('<I',raw,optional+108,16)
    struct.pack_into('<II',raw,optional+120,0x1000,40)
    struct.pack_into('<IIII',raw,optional+240+8,0x800,0x1000,0x800,0x200)
    struct.pack_into('<IIIII',raw,0x200,0x1100,0,0,0x1080,0x1180)
    raw[0x280:0x280+len(dll)+1]=dll+b'\0'
    struct.pack_into('<Q',raw,0x300,0x1200)
    raw[0x402:0x402+len(function)+1]=function+b'\0'
    return raw


class NativeProbeAuditTests(unittest.TestCase):
    def test_compile_definition_keeps_fixed_runtime_in_one_utf16_initializer_without_launch(self):
        root = Path('E:/owned fixture with spaces/鲸鱼🐋/python')
        with patch.object(probe.mxc, 'PYTHON_ROOT', root), \
             patch.object(probe.native, 'run', side_effect=AssertionError('No compiler or probe launch')):
            definition = probe.fixed_python_definition()
        self.assertTrue(definition.startswith('-DAURA_QA_PYTHON_EXE={'))
        self.assertNotIn('"', definition)
        self.assertNotIn("'", definition)
        literal = definition.split('=', 1)[1]
        units = json.loads('[' + literal[1:-1] + ']')
        self.assertEqual(units[-1], 0)
        self.assertTrue(all(type(unit) is int and 0 <= unit <= 65535 for unit in units))
        decoded = b''.join(unit.to_bytes(2, 'little') for unit in units[:-1]).decode('utf-16-le')
        self.assertEqual(decoded, str(root / 'python.exe'))

    def test_compile_definition_rejects_control_characters_in_host_pin(self):
        for control in ('\x00', '\r', '\n'):
            with self.subTest(control=ord(control)), patch.object(probe.mxc, 'PYTHON_ROOT', Path('E:/owned' + control + 'runtime')), \
                 patch.object(probe.native, 'run', side_effect=AssertionError('No compiler or probe launch')):
                with self.assertRaisesRegex(ValueError, 'INVALID_FIXED_QA_PYTHON_PATH'):
                    probe.fixed_python_definition()

    def test_default_never_uses_compiler_window_or_launcher(self):
        output=io.StringIO()
        with patch.object(probe.sys,'argv',['probe']), \
             patch.object(probe.native,'run',side_effect=AssertionError('launch')), \
             patch.object(probe,'_HiddenWindow',side_effect=AssertionError('window')), \
             redirect_stdout(output):
            self.assertEqual(0,probe.main())
        self.assertIn('NOT_REQUESTED',output.getvalue())

    def test_missing_pin_fails_before_any_compilation_or_native_api(self):
        with patch.object(probe.sys,'argv',['probe','--fixed-native-mitigation']), \
             patch.object(probe,'COMPILER_SHA256',''), \
             patch.object(probe.native,'run',side_effect=AssertionError('launch')):
            with self.assertRaisesRegex(ValueError,'COMPILER_NOT_VERIFIED'):probe.main()

    def test_fixed_exit_bits_are_observations_not_authority(self):
        flags=probe.decode_exit((19<<16)|1|2|8|16|64|128|512|1024|4096|8192)
        self.assertTrue(flags['self_disabled'] and flags['after_disabled'])
        self.assertFalse(flags['zero_clear_returned'] or flags['child_disabled'])
        self.assertEqual(19,flags['zero_clear_winerror'])
        self.assertNotIn('success',flags)
        for invalid in (True,-1,0x100000000,0x8000,None,{},'7'):
            with self.subTest(value=type(invalid).__name__):
                with self.assertRaises(ValueError):probe.decode_exit(invalid)

    def test_import_reader_accepts_only_bounded_kernel32_no_crt(self):
        self.assertEqual({'kernel32.dll':['ExitProcess']},probe.pe_imports(bytes(image())))
        for dll,function in ((b'user32.dll',b'ExitProcess'),(b'mscoree.dll',b'ExitProcess'),
                             (b'kernel32.dll',b'OpenProcess'),(b'kernel32.dll',b'CreateRemoteThread')):
            with self.subTest(dll=dll,function=function):
                with self.assertRaisesRegex(ValueError,'INVALID_PE'):probe.pe_imports(bytes(image(dll,function)))

    def test_import_reader_rejects_clr_tls_delayimports_ordinals_and_truncation(self):
        fixtures=[]
        for directory in (9,13,14):
            raw=image();struct.pack_into('<II',raw,0x98+112+directory*8,0x1000,16);fixtures.append(raw)
        raw=image();struct.pack_into('<Q',raw,0x300,1<<63|7);fixtures.append(raw)
        raw=image();struct.pack_into('<I',raw,0x200,0xfffffff0);fixtures.append(raw)
        fixtures.extend((image()[:100],bytearray(b'not a PE')))
        for i,raw in enumerate(fixtures):
            with self.subTest(case=i):
                with self.assertRaisesRegex(ValueError,'INVALID_PE'):probe.pe_imports(bytes(raw))

    def test_owned_observation_rejects_stale_malformed_and_non_boolean_metadata(self):
        values=[0x41555241,1,37,1,1,0,1114,0,0,0,0,0,0,1,0,0]
        report=probe.decode_observation(struct.pack('<16I',*values),37)
        self.assertIs(report['module_loaded'],False)
        self.assertEqual(1114,report['module_load_error'])
        self.assertIs(report['caption_read_attempted'],False)
        self.assertNotIn('nonce',report)
        for index,value in ((0,0),(1,2),(2,38),(3,2),(15,1)):
            rows=values.copy();rows[index]=value
            with self.subTest(index=index),self.assertRaisesRegex(ValueError,'INVALID_OBSERVATION'):
                probe.decode_observation(struct.pack('<16I',*rows),37)
        for raw,nonce in ((b'',37),(b'x'*65,37),(struct.pack('<16I',*values),True)):
            with self.assertRaisesRegex(ValueError,'INVALID_OBSERVATION'):probe.decode_observation(raw,nonce)


if __name__=='__main__':unittest.main()
