"""Independent disk/queue/JSONL review, using owned configuration and no renderer.

These tests exercise real config-file reads and coordinator guards, but do not
claim a native Office process or model was launched. Global local config is
never touched; ROOT/CONFIG redirects exist only inside this test process.
"""
from dataclasses import replace
from contextlib import ExitStack
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from desktop_pet import chat_controller as chat
from desktop_pet.agents import office_runtime as office
from desktop_pet.agents import service as service_module
from desktop_pet.agents.contracts import AgentError, AgentRequest
from desktop_pet.agents.service import AgentService
from desktop_pet.agents.settings import AgentSettings
from desktop_pet.agents.staged_input import StagedRunInput
from tests import test_host_queue_review as fixture
from tests import test_agent_chat_routing as qt_fixture
from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtWidgets import QApplication
from desktop_pet.agents.contracts import AgentResult
from desktop_pet.conversations.store import ConversationStore
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
import tempfile
from uuid import uuid4
from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
from agent_backend.nanobot.tests.test_worker import PYTHON as SDK_PYTHON

_UNCONFIRMED_TEST_OWNERS = []


class OfficeSnapshotIndependentReviewTests(unittest.TestCase):
    make_coordinator = fixture.HostQueueIndependentReviewTests.make_coordinator
    started = fixture.HostQueueIndependentReviewTests.started
    admit = fixture.HostQueueIndependentReviewTests.admit

    def setUp(self):
        fixture.HostQueueIndependentReviewTests.setUp(self)
        self.path = self.root / "agent_backend/nanobot/runtime/office_renderer.json"
        self.path.parent.mkdir(parents=True)
        self.config = {"engine": "libreoffice", "runtime": str(self.root / "vendor/runtime"),
            "scratch_parent": str(self.root / "private/scratch"),
            "pdftoppm": str(self.root / "poppler/bin/pdftoppm.exe")}
        self.patches = ExitStack(); self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(office, "_ROOT", self.root))
        self.patches.enter_context(patch.object(office, "_CONFIG", self.path))
        self.cached = SimpleNamespace(_agent_queue_policy=dict(self.policy))
        self.coordinator.configuration = self.current

    def current(self):
        return chat.ChatController._current_agent_queue_policy(self.cached)

    def write_config(self, value=None, *, raw=None):
        self.path.write_text(raw if raw is not None else json.dumps(value or self.config), encoding="utf-8")

    def prepared(self):
        capture = office.capture_office_runtime()
        return {**self.policy, "office_renderer_identity": capture.identity}, capture

    def queued(self):
        ack = self.admit(); self.coordinator.pump()
        context, request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        return ack, context, request

    def reserve(self, ack, context, request, prepared):
        with self.coordinator.reserve_guard(request, context, prepared=prepared):
            self.store.reserve_agent_execution(self.cid, ack["message_id"])
        handle = object()
        self.coordinator.worker_bound(request, context, handle)
        self.coordinator.service_entered(context)
        return handle

    def stage(self, frame):
        pipe = io.BytesIO()
        writer = StagedRunInput(pipe, stop_owned=lambda: None, cancellation_event=threading.Event())
        writer.start()
        self.addCleanup(lambda: self.assertTrue(writer.close()))
        writer.stage(json.dumps(frame, ensure_ascii=True).encode("ascii"))
        return pipe, writer

    def test_real_missing_file_created_in_remote_reservation_window_rejects_without_reserving(self):
        ack, context, request = self.queued()
        prepared, capture = self.prepared()
        self.assertEqual("", capture.identity)
        self.write_config()
        with self.assertRaises(AgentError):
            with self.coordinator.reserve_guard(request, context, prepared=prepared):
                self.fail("A newly selected renderer must not inherit the old missing-config scope")
        self.assertNotIn("agent_execution_started", self.store.read_message(self.cid, ack["message_id"])["metadata"])

    def test_real_present_file_removed_before_remote_reservation_rejects_stale_capture(self):
        self.write_config()
        ack, context, request = self.queued()
        prepared, capture = self.prepared()
        self.assertTrue(capture.identity)
        self.path.unlink()
        with self.assertRaises(AgentError):
            with self.coordinator.reserve_guard(request, context, prepared=prepared):
                self.fail("Missing config must not silently turn the prepared Office task into COM")
        self.assertNotIn("agent_execution_started", self.store.read_message(self.cid, ack["message_id"])["metadata"])

    def test_direct_local_missing_capture_cannot_commit_lf_after_renderer_is_selected(self):
        request = {"conversation_id": self.cid, "message_id": self.seed["id"], "cancel": threading.Event()}
        prepared, capture = self.prepared()
        with self.coordinator.reserve_guard(request, None, prepared=prepared): pass
        handle = object(); self.coordinator.worker_bound(request, None, handle)
        config, _, _ = AgentService._context({"api": self.policy["api"], "office_runtime_snapshot": capture},
            self.policy["key"], AgentRequest("owned direct fixture", "agent"), AgentSettings())
        self.assertNotIn("office_renderer", config)
        pipe, writer = self.stage({"type": "run", "config": config})
        self.write_config()
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request, None, handle): writer.commit()
        self.assertNotIn(b"\n", pipe.getvalue())

    def test_queued_staged_frame_stays_old_but_changed_disk_config_cannot_commit_lf(self):
        self.write_config()
        ack, context, request = self.queued()
        prepared, capture = self.prepared()
        handle = self.reserve(ack, context, request, prepared)
        config, _, _ = AgentService._context({"api": self.policy["api"], "office_runtime_snapshot": capture},
            self.policy["key"], AgentRequest("owned queued fixture", "agent"), AgentSettings())
        old_frame = json.loads(json.dumps(config))
        pipe, writer = self.stage({"type": "run", "config": config})
        self.write_config({**self.config, "runtime": str(self.root / "other/runtime")})
        self.assertNotEqual(capture.identity, self.current()["office_renderer_identity"])
        self.assertEqual(old_frame, config)
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request, context, handle): writer.commit()
        self.assertNotIn(b"\n", pipe.getvalue())

    def test_real_file_becoming_invalid_prevents_checkpoint_ack_and_does_not_change_qt_cache(self):
        self.write_config()
        ack, context, request = self.queued()
        prepared, _ = self.prepared()
        handle = self.reserve(ack, context, request, prepared)
        cached = dict(self.cached._agent_queue_policy)
        acknowledgements = []
        self.write_config(raw='{"engine":"libreoffice","secret":"SYNTHETIC_NEVER_PUBLIC"}')
        with self.assertRaises(AgentError) as caught:
            with self.coordinator.dispatch_guard(request, context, handle):
                acknowledgements.append({"type": "checkpoint_ack"})
        self.assertEqual([], acknowledgements)
        self.assertEqual(cached, self.cached._agent_queue_policy)
        self.assertNotIn("SYNTHETIC_NEVER_PUBLIC", str(caught.exception))

    def test_semantically_identical_disk_rewrite_preserves_scope_and_allows_exactly_one_lf(self):
        self.write_config()
        ack, context, request = self.queued()
        prepared, capture = self.prepared()
        handle = self.reserve(ack, context, request, prepared)
        pipe, writer = self.stage({"type": "run", "owned": True})
        self.write_config(raw=json.dumps(dict(reversed(list(self.config.items()))), indent=2))
        self.assertEqual(capture.identity, self.current()["office_renderer_identity"])
        with self.coordinator.dispatch_guard(request, context, handle): writer.commit()
        self.assertEqual(1, pipe.getvalue().count(b"\n"))
        self.assertTrue(pipe.getvalue().endswith(b"\n"))
        self.assertNotIn("office_renderer_identity", self.cached._agent_queue_policy)

    def test_invalid_present_file_is_fixed_error_before_any_process_creation_without_com_fallback(self):
        self.write_config(raw='{"engine":"com","key":"SYNTHETIC_CONFIG_SECRET"}')
        service = AgentService()
        self.addCleanup(service.shutdown)
        with patch.object(service_module.subprocess, "Popen", side_effect=AssertionError("Must not launch")) as launch:
            with self.assertRaises(AgentError) as caught:
                service.execute(replace(AgentSettings(), enabled=True), self.policy["key"],
                    AgentRequest("ordinary owned file task", "agent"), {"api": self.policy["api"]})
        self.assertEqual("office_runtime_invalid", caught.exception.code)
        self.assertEqual(0, launch.call_count)
        self.assertNotIn("SYNTHETIC_CONFIG_SECRET", str(caught.exception))
        self.assertIsNone(self.current())

    def test_fake_snapshot_properties_are_not_consulted_or_translated_into_worker_authority(self):
        class Untrusted:
            @property
            def configuration(self): raise AssertionError("SYNTHETIC_MUST_NOT_BE_READ")
            @property
            def identity(self): raise AssertionError("SYNTHETIC_MUST_NOT_BE_READ")
        forged = object.__new__(office.OfficeRuntimeSnapshot)
        for value in (Untrusted(), forged, {"_issuer": "host", "_encoded": json.dumps(self.config)}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(AgentError) as caught:
                AgentService._context({"api": self.policy["api"], "office_runtime_snapshot": value},
                    self.policy["key"], AgentRequest("owned fixture", "agent"), AgentSettings())
            self.assertEqual("office_runtime_invalid", caught.exception.code)
            self.assertNotIn("SYNTHETIC_MUST_NOT_BE_READ", str(caught.exception))

    def test_fresh_policy_does_not_access_qt_settings_or_credential_provider(self):
        class NoQtReads:
            def __getattr__(self, name): raise AssertionError("Background policy queried a GUI-owned provider")
        self.cached._settings_manager = NoQtReads()
        before = dict(self.cached._agent_queue_policy)
        self.assertEqual("", self.current()["office_renderer_identity"])
        self.write_config()
        self.assertTrue(self.current()["office_renderer_identity"])
        self.assertEqual(before, self.cached._agent_queue_policy)


class _RecordingHostService(AgentService):
    """Run the real context validator; no process or model is started."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.frames = []
        self.blocked = False
        self.check_dispatch = False

    def execute(self, settings, key, request, context, **options):
        self.calls.append(context)
        self.frames.append(self._context(context, key, request, settings)[0])
        if self.check_dispatch:
            handle = object()
            options["worker_lifecycle_callback"](handle)
            try:
                with options["worker_dispatch_guard"](handle):
                    self.fail_publication = True
            except AgentError:
                self.blocked = True
                raise
        return AgentResult("Owned context fixture completed.", "neutral")


class OfficeControllerFrameIndependentReviewTests(unittest.TestCase):
    spin = qt_fixture.AgentChatRoutingTests.spin

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        owned_parent = Path(service_module.AGENT_WORKSPACE_ROOT)
        owned_parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-office-frame-review-", dir=owned_parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "agent_backend/nanobot/runtime/office_renderer.json"
        self.path.parent.mkdir(parents=True)
        self.config = {"engine": "libreoffice", "runtime": str(self.root / "vendor/runtime"),
            "scratch_parent": str(self.root / "private/scratch"),
            "pdftoppm": str(self.root / "poppler/bin/pdftoppm.exe")}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(office, "_ROOT", self.root))
        self.patches.enter_context(patch.object(office, "_CONFIG", self.path))
        self.settings = SettingsManager(QSettings(str(self.root / "owned.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=qt_fixture.MemoryVault()))
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "owned-unused-model", 5))
        self.settings.save_api_key("SYNTHETIC_FRAME_REVIEW_KEY")
        self.settings.save_agent_settings(replace(AgentSettings(), enabled=True,
            workspace=str(self.root / "workspace")))
        self.api = qt_fixture.FakeApi()
        self.controllers = []

    def controller(self, service):
        controller = chat.ChatController(self.settings, self.api,
            store=ConversationStore(self.root / ("chat-" + str(len(self.controllers)) + ".sqlite3")),
            agent_service=service, metadata_client_factory=lambda: None)
        self.controllers.append(controller)
        self.spin(lambda: not controller.history_loading)
        return controller

    def tearDown(self):
        for controller in self.controllers: controller.shutdown()
        self.spin(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for controller in self.controllers: controller.deleteLater()
        self.app.processEvents()
        self.patches.close()
        self.temporary.cleanup()

    def test_actual_controller_host_frame_and_reserved_policy_share_one_immutable_capture(self):
        service = _RecordingHostService(); controller = self.controller(service)
        observed = []
        original = controller._agent_queue.reserve_guard
        @contextmanager
        def recording(*args, prepared=None, **kwargs):
            observed.append(dict(prepared))
            with original(*args, prepared=prepared, **kwargs): yield
        with patch.object(controller._agent_queue, "reserve_guard", side_effect=recording):
            self.assertTrue(controller.send("检查已有固定数据", agent_mode=True))
            self.spin(lambda: not controller.busy)
        self.assertEqual(1, len(service.calls))
        snapshot = service.calls[0]["office_runtime_snapshot"]
        self.assertTrue(office.valid_office_runtime_snapshot(snapshot))
        self.assertEqual(snapshot.identity, observed[0]["office_renderer_identity"])
        self.assertEqual(snapshot.identity, controller._current_agent_queue_policy()["office_renderer_identity"])
        self.assertEqual(snapshot.configuration, service.frames[0]["office_renderer"])
        self.assertNotIn("office_runtime_snapshot", json.dumps(controller.store.load_messages(controller.current_conversation_id)))
        self.assertEqual([], self.api.calls)

    def test_plugin_client_receives_plain_context_without_host_snapshot_authority(self):
        service = qt_fixture.FakeAgent(); controller = self.controller(service)
        self.assertTrue(controller.send("检查已有固定数据", agent_mode=True))
        self.spin(lambda: not controller.busy)
        self.assertEqual(1, len(service.calls))
        self.assertNotIn("office_runtime_snapshot", service.calls[0]["context"])
        self.assertNotIn("office_renderer", service.calls[0]["context"])
        self.assertEqual([], self.api.calls)

    def test_real_controller_replacement_after_reserve_preserves_frame_but_blocks_dispatch(self):
        service = _RecordingHostService(); service.check_dispatch = True
        controller = self.controller(service)
        original = controller._agent_queue.reserve_guard
        @contextmanager
        def replace_after_reserve(*args, **kwargs):
            with original(*args, **kwargs): yield
            self.path.write_text(json.dumps({**self.config, "runtime": str(self.root / "replacement/runtime")}), encoding="utf-8")
        with patch.object(controller._agent_queue, "reserve_guard", side_effect=replace_after_reserve):
            self.assertTrue(controller.send("检查已有固定数据", agent_mode=True))
            self.spin(lambda: not controller.busy)
        self.assertEqual(1, len(service.calls))
        self.assertTrue(service.blocked)
        self.assertFalse(getattr(service, "fail_publication", False))
        self.assertEqual(str((self.root / "vendor/runtime").resolve()), service.frames[0]["office_renderer"]["runtime"])
        self.assertEqual({}, service._processes)
        self.assertEqual([], self.api.calls)


@unittest.skipUnless(SDK_PYTHON.is_file(), "Isolated SDK is unavailable")
class OfficeOrdinaryToolsIndependentSdkTests(unittest.TestCase):
    def test_selected_but_scope_conflicting_office_does_not_globally_block_ordinary_file_task(self):
        parent = Path(service_module.AGENT_WORKSPACE_ROOT); parent.mkdir(exist_ok=True)
        workspace_owner = tempfile.TemporaryDirectory(prefix="office-ordinary-sdk-review-", dir=parent)
        config_owner = tempfile.TemporaryDirectory(prefix="office-ordinary-config-review-")
        workspace, config_root = Path(workspace_owner.name), Path(config_owner.name)
        path = config_root / "agent_backend/nanobot/runtime/office_renderer.json"
        path.parent.mkdir(parents=True)
        # The selected renderer cannot bind safely inside model scope, and its
        # runtime does not exist. File-only tools should still work unchanged.
        config = {"engine": "libreoffice", "runtime": str(workspace / "unavailable-native-runtime"),
            "scratch_parent": str(config_root / "private/scratch"),
            "pdftoppm": str(config_root / "poppler/bin/pdftoppm.exe")}
        path.write_text(json.dumps(config), encoding="utf-8")
        summary = "Created ordinary.txt with content plain and read it back."
        model = SeparateGoalModel([("write_file", {"path": "ordinary.txt", "content": "plain"}),
            ("read_file", {"path": "ordinary.txt"}),
            ("finish_task", {"status": "completed", "summary": summary}), summary], judge_mode="optimistic")
        service, handles, closed = AgentService(), [], False
        try:
            settings = replace(AgentSettings(), enabled=True, computer_enabled=False, timeout_seconds=25,
                max_steps=0, workspace=str(workspace), python_executable=str(SDK_PYTHON))
            with patch.object(office, "_ROOT", config_root), patch.object(office, "_CONFIG", path):
                result = service.execute(settings, "SYNTHETIC_ORDINARY_REVIEW_KEY",
                    AgentRequest("Create ordinary.txt containing exactly plain, read it back and report the result.", "agent"),
                    {"api": {"base_url": model.base_url, "model": "owned-ordinary-sdk"}, "messages": [],
                     "conversation_id": str(uuid4())}, worker_lifecycle_callback=handles.append)
            closed = bool(handles and service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
            self.assertTrue(closed)
            self.assertEqual("completed", result.status)
            self.assertEqual("plain", (workspace / "ordinary.txt").read_text(encoding="utf-8"))
            self.assertFalse((workspace / "unavailable-native-runtime").exists())
            self.assertEqual(4, len(model.planner_calls))
            self.assertEqual(1, len(model.judge_calls))
            self.assertEqual([], model.errors)
        finally:
            service.shutdown(); model.close()
            if closed:
                workspace_owner.cleanup(); config_owner.cleanup()
            else:
                _UNCONFIRMED_TEST_OWNERS.append((service, workspace_owner, config_owner))


if __name__ == "__main__": unittest.main()
