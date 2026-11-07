"""Host configuration capture only; no native/model launch or real settings."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import pickle
import tempfile
import unittest
from unittest.mock import patch

from desktop_pet.agents import execution_runtime as subject


class ExecutionRuntimeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-project-config-test-")
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name); self.path = self.root/subject._RELATIVE
        self.root_patch = patch.object(subject, "_ROOT", self.root); self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.path_patch = patch.object(subject, "_CONFIG", self.path); self.path_patch.start(); self.addCleanup(self.path_patch.stop)

    def write(self, value):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_missing_is_disabled_empty_identity_and_does_not_create_config(self):
        value = subject.capture_execution_runtime()
        self.assertIsNone(value.configuration)
        self.assertEqual("", value.identity)
        self.assertFalse(self.path.exists())

    def test_enabled_capture_uses_only_fixed_policy_and_executable_pins(self):
        from agent_backend.nanobot.mxc_execution import MxcPolicy, CODEX_SHA256
        self.write({"engine": "mxc", "enabled": True})
        value = subject.capture_execution_runtime()
        self.assertTrue(subject.valid_execution_runtime_snapshot(value))
        self.assertTrue(value.configuration["enabled"])
        self.assertEqual(asdict(MxcPolicy()), value.configuration["policy"])
        self.assertEqual(CODEX_SHA256, value.configuration["runtime_pins"]["codex_sha256"])
        self.assertEqual(64, len(value.identity))

    def test_present_disabled_has_a_bound_identity_and_enabling_changes_it(self):
        self.write({"engine": "mxc", "enabled": False}); disabled = subject.capture_execution_runtime()
        self.assertFalse(disabled.configuration["enabled"])
        self.assertNotEqual("", disabled.identity)
        self.write({"engine": "mxc", "enabled": True})
        self.assertNotEqual(disabled.identity, subject.capture_execution_runtime().identity)

    def test_captured_policy_does_not_follow_file_or_returned_dictionary_edits(self):
        self.write({"engine": "mxc", "enabled": True}); captured = subject.capture_execution_runtime()
        self.write({"engine": "mxc", "enabled": False})
        frame = captured.configuration; frame["enabled"] = False; frame["policy"]["timeout_seconds"] = 99999
        self.assertTrue(captured.configuration["enabled"])
        self.assertEqual(30, captured.configuration["policy"]["timeout_seconds"])
        with self.assertRaises(AttributeError): captured._identity = "0"*64
        with self.assertRaises(TypeError): pickle.dumps(captured)

    def test_invalid_fields_engine_or_non_boolean_are_explicit_errors(self):
        for value in ({"engine":"mxc","enabled":1}, {"engine":"shell","enabled":True},
                      {"engine":"mxc","enabled":True,"python":"C:/untrusted.exe"},
                      {"engine":"mxc","enabled":True,"policy":{"network":True}}, None, []):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(subject.ExecutionRuntimeConfigurationError): subject.capture_execution_runtime()

    def test_duplicate_members_and_oversized_file_are_rejected(self):
        self.path.parent.mkdir(exist_ok=True)
        for raw in (b'{"engine":"mxc","enabled":false,"enabled":true}', b" "*(subject._LIMIT+1)):
            self.path.write_bytes(raw)
            with self.assertRaises(subject.ExecutionRuntimeConfigurationError): subject.capture_execution_runtime()

    def test_plain_model_values_cannot_mint_host_snapshot(self):
        for value in ({"engine":"mxc","enabled":True}, True, None, object()):
            self.assertFalse(subject.valid_execution_runtime_snapshot(value))
        with self.assertRaises(TypeError): subject.ExecutionRuntimeSnapshot({"enabled":True})

    def test_existing_hardlink_is_rejected_without_changing_target(self):
        self.path.parent.mkdir(exist_ok=True)
        original = self.root/"owned-original.json"; raw = b'{"engine":"mxc","enabled":true}'
        original.write_bytes(raw); os.link(original, self.path)
        with self.assertRaises(subject.ExecutionRuntimeConfigurationError): subject.capture_execution_runtime()
        self.assertEqual(raw, original.read_bytes())


if __name__ == "__main__": unittest.main()
