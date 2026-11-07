"""Host configuration capture stays bound despite later file/dictionary edits."""
import pickle
import unittest
from unittest.mock import patch

from desktop_pet.agents import office_runtime as runtime


class OfficeRuntimeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.config = {"engine": "libreoffice", "runtime": "E:/trusted/runtime", "scratch_parent": "E:/private/scratch", "pdftoppm": "E:/trusted/poppler.exe"}

    def capture(self, config):
        with patch.object(runtime, "load_office_runtime", return_value=config): return runtime.capture_office_runtime()

    def test_later_source_and_frame_mutation_cannot_change_captured_host_policy(self):
        captured = self.capture(self.config)
        original = captured.identity
        self.config["runtime"] = "E:/changed/runtime"
        frame = captured.configuration; frame["scratch_parent"] = "E:/changed/scratch"
        self.assertEqual(original, captured.identity)
        self.assertEqual("E:/trusted/runtime", captured.configuration["runtime"])
        self.assertEqual("E:/private/scratch", captured.configuration["scratch_parent"])
        self.assertNotEqual(original, self.capture(self.config).identity)

    def test_missing_configuration_retains_empty_legacy_identity(self):
        captured = self.capture(None)
        self.assertTrue(runtime.valid_office_runtime_snapshot(captured))
        self.assertIsNone(captured.configuration)
        self.assertEqual("", captured.identity)

    def test_external_data_or_serialized_values_cannot_mint_host_snapshot(self):
        for value in (None, {}, self.config, True, object()):
            self.assertFalse(runtime.valid_office_runtime_snapshot(value))
        with self.assertRaises(TypeError): runtime.OfficeRuntimeSnapshot()
        captured = self.capture(self.config)
        with self.assertRaises(AttributeError): captured._identity = "0"*64
        with self.assertRaises(TypeError): pickle.dumps(captured)


if __name__ == "__main__": unittest.main()
