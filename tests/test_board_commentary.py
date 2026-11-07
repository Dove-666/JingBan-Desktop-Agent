"""Move selection preserves API preferences and cancels obsolete transports."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.games.board_commentary import BoardGameCommentary
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer


class Game(QObject):
    changed = Signal(dict)
    commentary_requested = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.owner_thread = threading.get_ident()
        self.state = {"revision": 0, "position": "opening", "rule": "freestyle", "commentary": []}

    def snapshot(self):
        if threading.get_ident() != self.owner_thread:
            raise AssertionError("A worker accessed the Qt controller")
        return dict(self.state)

    def update(self, **values):
        self.state.update(values)
        self.changed.emit(self.snapshot())


class Settings:
    def __init__(self, url="http://127.0.0.1:9/v1", effort="default"):
        self.owner_thread = threading.get_ident()
        self.api = ApiSettings(base_url=url, model="owned-board-test", timeout_seconds=30,
                               reasoning_effort=effort)

    def load_api_settings(self):
        if threading.get_ident() != self.owner_thread:
            raise AssertionError("A worker accessed settings")
        return self.api

    def get_api_key(self):
        if threading.get_ident() != self.owner_thread:
            raise AssertionError("A worker accessed credentials")
        return "owned-board-test-key"


class MoveClient:
    def __init__(self, block=False, fail=False):
        self.requests = []
        self.cancelled = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block, self.fail = block, fail
        self.closed = False

    def request_text(self, settings, _key, messages, **kwargs):
        self.requests.append((settings, messages, kwargs))
        self.entered.set()
        if self.block and not self.release.wait(timeout=3):
            raise AssertionError("Transport was not released")
        if self.fail:
            raise RuntimeError("Owned transport failure")
        return '{"move":"H8","reply":"","emotion":"neutral"}'

    def cancel_request(self, event):
        self.cancelled.append(event)
        self.release.set()

    def shutdown(self):
        self.closed = True
        self.release.set()


def identity(state):
    return state["revision"], state["position"], state["rule"]


class BoardCommentaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.game = Game()
        self.services, self.workers = [], []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
        for worker in self.workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        for service in self.services:
            service.deleteLater()
        self.game.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def service(self, settings=None, move_client=None):
        service = BoardGameCommentary(settings or Settings(), self.game,
            context=lambda state: {"position": state["position"], "rule": state["rule"]},
            identity=identity, game_prompt="Owned game prompt", move_prompt="Owned move prompt",
            move_client=move_client)
        self.services.append(service)
        return service

    def start_selection(self, selector, event):
        results = []
        worker = threading.Thread(target=lambda: results.append(
            selector([{"move": "H8"}], cancellation_event=event)), name="owned-board-selection")
        self.workers.append(worker)
        worker.start()
        return worker, results

    def test_preserves_reasoning_setting_and_only_bounds_move_timeout(self):
        for effort in ("default", "high", "none"):
            with self.subTest(effort=effort):
                settings, client = Settings(effort=effort), MoveClient()
                service = self.service(settings, client)
                selector = service.move_selector(self.game.snapshot())
                worker, results = self.start_selection(selector, threading.Event())
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0]["move"], "H8")
                captured = client.requests[0][0]
                self.assertEqual(captured, replace(settings.api, timeout_seconds=12))
                self.assertEqual(settings.api.timeout_seconds, 30)

    def test_position_change_actively_cancels_actual_sdk_transport(self):
        with MockApiServer() as server:
            server.enqueue('{"move":"H8","reply":"old reply","emotion":"happy"}', delay=4)
            service = self.service(Settings(url=server.base_url))
            selector = service.move_selector(self.game.snapshot())
            event = threading.Event()
            with patch.object(service.move_client, "cancel_request",
                              wraps=service.move_client.cancel_request) as cancel:
                worker, results = self.start_selection(selector, event)
                self.assertTrue(server.received.wait(timeout=3))
                self.game.update(position="after a move", revision=1)
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), "Old HTTP kept waiting for the delayed response")
                self.assertEqual(results, [None])
                self.assertTrue(event.is_set())
                cancel.assert_called_once_with(event)
            self.assertNotIn("reasoning_effort", server.requests[0]["body"])

    def test_selector_captured_before_change_is_rejected_before_transport(self):
        for changed in ({"revision": 1}, {"position": "restored position"}, {"rule": "standard"}):
            with self.subTest(changed=changed):
                client = MoveClient()
                service = self.service(move_client=client)
                selector = service.move_selector(self.game.snapshot())
                self.game.update(**changed)
                worker, results = self.start_selection(selector, threading.Event())
                worker.join(timeout=1)
                self.assertEqual(results, [None])
                self.assertEqual(client.requests, [])

    def test_ordinary_same_position_updates_do_not_cancel_live_selection(self):
        client = MoveClient(block=True)
        service = self.service(move_client=client)
        selector = service.move_selector(self.game.snapshot())
        event = threading.Event()
        worker, results = self.start_selection(selector, event)
        self.assertTrue(client.entered.wait(timeout=1))
        self.game.update(status="thinking", commentary=[{"role": "user", "text": "hello"}])
        self.assertEqual(client.cancelled, [])
        self.assertFalse(event.is_set())
        client.release.set()
        worker.join(timeout=1)
        self.assertEqual(results[0]["move"], "H8")

    def test_shutdown_cancels_pending_move_and_rejects_unused_selector(self):
        client = MoveClient(block=True)
        service = self.service(move_client=client)
        selector = service.move_selector(self.game.snapshot())
        event = threading.Event()
        worker, results = self.start_selection(selector, event)
        self.assertTrue(client.entered.wait(timeout=1))
        service.shutdown()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(client.cancelled, [event])
        self.assertTrue(event.is_set())
        self.assertTrue(client.closed)
        self.assertIsNone(selector([{"move": "H8"}], cancellation_event=threading.Event()))
        self.assertEqual(len(client.requests), 1)

    def test_completed_or_failed_transports_are_not_later_cancelled(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                client = MoveClient(fail=fail)
                service = self.service(move_client=client)
                selector = service.move_selector(self.game.snapshot())
                event = threading.Event()
                worker, results = self.start_selection(selector, event)
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0] is None, fail)
                self.game.update(revision=self.game.state["revision"]+1)
                self.assertEqual(client.cancelled, [])
                self.assertFalse(event.is_set())


if __name__ == "__main__":
    unittest.main()
