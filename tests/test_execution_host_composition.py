"""Host capture/queue/checkpoint fences; no credential, Qt or native launch."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import execution_runtime as runtime
from desktop_pet.agents.service import AgentService
from desktop_pet.agents.contracts import AgentError, AgentRequest
from desktop_pet.agents.settings import AgentSettings
from desktop_pet.agents.checkpoints import scope_fingerprint
from desktop_pet.agents.coordinator import settings_identity, CoordinatorError
from desktop_pet.settings_manager import ApiSettings


class ExecutionHostCompositionTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-execution-host-test-")
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name); self.private = self.root/"private"
        self.config = self.private/"runtime.json"
        for name, value in (("_ROOT", self.private), ("_CONFIG", self.config)):
            mocked = patch.object(runtime, name, value); mocked.start(); self.addCleanup(mocked.stop)
        self.api = ApiSettings("https://synthetic.invalid/v1", "owned-model", 5)
        self.settings = replace(AgentSettings(), enabled=True, tools_enabled=True, computer_enabled=False)
        self.key = "SYNTHETIC_CONFIG_TEST_KEY"
        self.cid, self.generation = str(uuid4()), str(uuid4())

    def write(self, enabled=True):
        self.private.mkdir(exist_ok=True)
        self.config.write_text(json.dumps({"engine":"mxc", "enabled":enabled}), encoding="utf-8")
        return runtime.capture_execution_runtime()

    def context(self, **extra):
        return {"api":self.api, "messages":[], "conversation_id":self.cid, **extra}

    def test_service_captures_config_and_identity_and_rejects_plain_dictionary_authority(self):
        snapshot = self.write()
        config, _, _ = AgentService._context(self.context(execution_runtime_snapshot=snapshot), self.key, AgentRequest("Owned task"), self.settings)
        self.assertTrue(config["project_execution"]["enabled"])
        self.assertEqual(snapshot.identity, config["project_execution_identity"])
        with self.assertRaises(AgentError) as raised:
            AgentService._context(self.context(execution_runtime_snapshot={"enabled":True}), self.key, AgentRequest("Owned task"), self.settings)
        self.assertEqual("execution_runtime_invalid", raised.exception.code)

    def test_legacy_call_without_cid_keeps_regular_tools_but_has_no_project_wire(self):
        snapshot = self.write()
        config, _, _ = AgentService._context({"api":self.api, "messages":[], "execution_runtime_snapshot":snapshot}, self.key, AgentRequest("Owned task"), self.settings)
        self.assertNotIn("project_execution", config)
        self.assertEqual(snapshot.identity, config["project_execution_identity"])

    def test_missing_config_adds_no_new_wire_or_checkpoint_identity(self):
        config, _, _ = AgentService._context(self.context(), self.key, AgentRequest("Owned task"), self.settings)
        self.assertNotIn("project_execution", config)
        self.assertNotIn("project_execution_identity", config)
        before = scope_fingerprint(self.settings, config, secrets=(self.key,), generation=self.generation)
        self.assertEqual(before, scope_fingerprint(self.settings, {**config,"project_execution_identity":""}, secrets=(self.key,), generation=self.generation))
        self.assertNotEqual(before, scope_fingerprint(self.settings, {**config,"project_execution_identity":"a"*64}, secrets=(self.key,), generation=self.generation))

    def test_queue_scope_changes_with_capture_and_rejects_non_hash_identity(self):
        configuration = {"api":self.api, "key":self.key, "agent":self.settings, "gui_key":""}
        args = {"peer":"a"*64, "authorization":"b"*64, "generation":self.generation}
        original = settings_identity(configuration, **args)
        self.assertEqual(original, settings_identity({**configuration,"project_execution_identity":""}, **args))
        self.assertNotEqual(original, settings_identity({**configuration,"project_execution_identity":self.write().identity}, **args))
        for bad in (True, {}, "not-a-hash"):
            with self.assertRaises(CoordinatorError): settings_identity({**configuration,"project_execution_identity":bad}, **args)

    def test_invalid_local_config_is_a_fixed_error_without_raw_file_values(self):
        self.private.mkdir()
        self.config.write_text('{"engine":"mxc","enabled":true,"api_key":"SYNTHETIC_PRIVATE_VALUE"}', encoding="utf-8")
        with self.assertRaises(AgentError) as raised:
            AgentService._context(self.context(), self.key, AgentRequest("Owned task"), self.settings)
        self.assertEqual("execution_runtime_invalid", raised.exception.code)
        self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", str(raised.exception))

    def test_private_policy_is_protected_before_file_exists_even_without_guardian(self):
        workspace = self.root/"workspace"; workspace.mkdir()
        service = AgentService(); self.addCleanup(service.shutdown)
        self.assertIsNone(service._guardian_config)
        service._check_guardian_scope(str(workspace))
        for value in (self.root, self.private, self.config):
            with self.assertRaises(AgentError) as raised:
                service._check_guardian_scope(str(workspace), extra_grant={"path":str(value)})
            self.assertEqual("guardian_scope_conflict", raised.exception.code)
        self.assertFalse(self.private.exists())

    def test_direct_dispatch_policy_guard_rechecks_actual_file_instead_of_old_capture(self):
        AgentService._check_execution_policy("")
        snapshot = self.write()
        AgentService._check_execution_policy(snapshot.identity)
        with self.assertRaises(AgentError) as changed:
            AgentService._check_execution_policy("")
        self.assertEqual("execution_runtime_changed", changed.exception.code)
        self.write(enabled=False)
        with self.assertRaises(AgentError) as changed:
            AgentService._check_execution_policy(snapshot.identity)
        self.assertEqual("execution_runtime_changed", changed.exception.code)


if __name__ == "__main__": unittest.main()
