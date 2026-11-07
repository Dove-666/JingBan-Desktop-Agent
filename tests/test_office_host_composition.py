"""Renderer capture binds host policy/prepared/frame without phone authority."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet import chat_controller as chat
from desktop_pet.agents import office_runtime as office
from desktop_pet.agents.coordinator import settings_identity, CoordinatorError
from desktop_pet.agents.contracts import AgentError, AgentRequest, AgentValidationError
from desktop_pet.agents.service import AgentService
from desktop_pet.agents.settings import AgentSettings
from tests import test_host_queue_review as queue_fixtures


class OfficeHostCompositionTests(unittest.TestCase):
    def setUp(self):
        self.api = {"base_url": "https://scope.invalid/v1", "model": "scope-model"}
        self.policy = {"api": self.api, "agent": {"enabled": True, "workspace": "E:/workspace"},
            "key": "SYNTHETIC_SCOPE_KEY", "gui_key": ""}
        self.scope = {"peer": "a"*64, "authorization": "b"*64,
            "generation": "00000000-0000-0000-0000-000000000001"}
        self.config = {"engine": "libreoffice", "runtime": "E:/trusted/runtime", "scratch_parent": "E:/private/scratch", "pdftoppm": "E:/trusted/poppler.exe"}

    def capture(self, value):
        with patch.object(office, "load_office_runtime", return_value=value): return office.capture_office_runtime()

    def test_no_renderer_preserves_known_preupgrade_scope_digest(self):
        expected = "f07489fbf579fa4ad9f8d81ae2a34e8da27463a2c097e70161586f7b691001f3"
        self.assertEqual(expected, settings_identity(self.policy, **self.scope))
        self.assertEqual(expected, settings_identity({**self.policy, "office_renderer_identity": ""}, **self.scope))

    def test_changed_renderer_identity_changes_host_scope_and_malformed_identity_is_rejected(self):
        base = settings_identity(self.policy, **self.scope)
        a = settings_identity({**self.policy, "office_renderer_identity": "a"*64}, **self.scope)
        b = settings_identity({**self.policy, "office_renderer_identity": "b"*64}, **self.scope)
        self.assertEqual(3, len({base, a, b}))
        for value in (None, {}, True, "A"*64, "a"*63):
            with self.subTest(value=value), self.assertRaises(CoordinatorError):
                settings_identity({**self.policy, "office_renderer_identity": value}, **self.scope)

    def test_worker_frame_uses_same_host_capture_as_prepared_policy_without_rereading(self):
        captured = self.capture(self.config)
        with patch.object(office, "capture_office_runtime", side_effect=AssertionError("Must retain prepared capture")):
            config, _, _ = AgentService._context({"api": self.api, "office_runtime_snapshot": captured},
                "SYNTHETIC_SCOPE_KEY", AgentRequest("owned fixture", "agent"), AgentSettings())
        self.assertEqual(self.config, config["office_renderer"])
        config["office_renderer"]["runtime"] = "CHANGED"
        self.assertEqual("E:/trusted/runtime", captured.configuration["runtime"])

    def test_context_without_explicit_capture_loads_host_configuration_and_missing_stays_legacy(self):
        for value in (None, self.config):
            with self.subTest(value=value), patch.object(office, "load_office_runtime", return_value=value):
                config, _, _ = AgentService._context({"api": self.api}, "SYNTHETIC_SCOPE_KEY",
                    AgentRequest("owned fixture", "agent"), AgentSettings())
                self.assertEqual(value, config.get("office_renderer"))

    def test_model_or_rpc_dictionary_cannot_supply_a_renderer_or_forge_host_capture(self):
        with self.assertRaises(AgentValidationError):
            AgentService._context({"api": self.api, "office_renderer": self.config}, "SYNTHETIC_SCOPE_KEY",
                AgentRequest("owned fixture", "agent"), AgentSettings())
        for value in (self.config, None, True):
            with self.subTest(value=value), self.assertRaises(AgentError) as caught:
                AgentService._context({"api": self.api, "office_runtime_snapshot": value}, "SYNTHETIC_SCOPE_KEY",
                    AgentRequest("owned fixture", "agent"), AgentSettings())
            self.assertEqual("office_runtime_invalid", caught.exception.code)

    def test_background_policy_getter_samples_only_host_runtime_without_mutating_qt_cache(self):
        holder = SimpleNamespace(_agent_queue_policy=dict(self.policy))
        for config in (self.config, {**self.config, "scratch_parent": "E:/new-private"}):
            captured = self.capture(config)
            with patch.object(chat, "capture_office_runtime", return_value=captured):
                result = chat.ChatController._current_agent_queue_policy(holder)
            self.assertEqual(captured.identity, result["office_renderer_identity"])
        self.assertNotIn("office_renderer_identity", holder._agent_queue_policy)

    def test_invalid_host_renderer_revokes_policy_without_changing_cached_api_values(self):
        holder = SimpleNamespace(_agent_queue_policy=dict(self.policy))
        with patch.object(chat, "capture_office_runtime", side_effect=office.OfficeRuntimeConfigurationError()):
            self.assertIsNone(chat.ChatController._current_agent_queue_policy(holder))
        self.assertEqual(self.policy, holder._agent_queue_policy)


class OfficeQueuedDispatchFenceTests(unittest.TestCase):
    setUp = queue_fixtures.HostQueueIndependentReviewTests.setUp
    make_coordinator = queue_fixtures.HostQueueIndependentReviewTests.make_coordinator
    started = queue_fixtures.HostQueueIndependentReviewTests.started
    admit = queue_fixtures.HostQueueIndependentReviewTests.admit

    def test_renderer_change_after_reservation_blocks_the_actual_host_dispatch_guard(self):
        self.policy["office_renderer_identity"] = "a"*64
        ack = self.admit(); self.coordinator.pump()
        context, request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        with self.coordinator.reserve_guard(request, context, prepared=self.policy): pass
        self.store.reserve_agent_execution(self.cid, ack["message_id"])
        handle = object(); self.coordinator.worker_bound(request, context, handle)
        self.coordinator.service_entered(context)
        with self.coordinator.dispatch_guard(request, context, handle): pass
        self.policy["office_renderer_identity"] = "b"*64
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request, context, handle):
                self.fail("A new native renderer cannot authorize the old reserved task")


if __name__ == "__main__": unittest.main()
