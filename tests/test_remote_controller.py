"""Remote authorization, UI-thread dispatch and real local chat persistence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSettings, QThreadPool
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.remote.controller import RemoteController
from desktop_pet.remote.settings import RemotePreferences
from desktop_pet.settings_manager import SettingsManager, CredentialStore, ApiSettings
from tests.mock_api import MockApiServer
from tests.test_settings import FakeVault


PEER = "a" * 64
OTHER_PEER = "b" * 64


def spin_until(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        QApplication.processEvents()
        QTest.qWait(3)
        time.sleep(.002)
    if not condition():
        raise AssertionError("isolated remote operation timed out")


def image_params():
    image = QImage(24, 16, QImage.Format.Format_ARGB32)
    image.fill(QColor("#75b8f4"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise AssertionError("test image encoding failed")
    return {"filename": "../phone/photo.png", "mime_type": "image/png",
            "data_base64": base64.b64encode(bytes(data)).decode("ascii")}


class FakeTransport:
    def __init__(self, secret_hex, on_request, on_status):
        self.secret = secret_hex
        self.request_callback = on_request
        self.status_callback = on_status
        self.stopped = False

    def start(self):
        self.status_callback({"state": "listening", "endpoint_id": "c" * 64, "ticket": "test-ticket"})

    def stop(self):
        self.stopped = True


class PetStub:
    def __init__(self):
        self.expression = "neutral"
        self.calls = []
        self.thread_ids = []

    def set_expression(self, value, **kwargs):
        self.expression = value
        self.calls.append(("expression", value))
        self.thread_ids.append(threading.get_ident())

    def set_scale(self, value):
        self.calls.append(("scale", value))

    def respond(self, value):
        self.calls.append(("say", value))

    def animation_catalog(self):
        return [("think", "thinking", "available")]

    def play_asset_animation(self, value):
        self.calls.append(("animation", value))
        return True


class RemoteControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-remote-controller-")
        self.path = Path(self.directory.name)
        self.vault = FakeVault()
        self.settings = SettingsManager(QSettings(str(self.path / "settings.ini"), QSettings.Format.IniFormat),
                                        CredentialStore(backend=self.vault))
        self.chat = ChatController(self.settings)
        self.pet = PetStub()
        self.session = SimpleNamespace(settings=self.settings, chat=self.chat, pet_controller=self.pet,
                                      pet=SimpleNamespace(setVisible=lambda value: None),
                                      tray=SimpleNamespace(available=True, refresh=lambda: None))
        self.remote = RemoteController(self.session, transport_factory=FakeTransport)
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)

    def tearDown(self):
        self.remote.shutdown()
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.remote.deleteLater()
        self.chat.deleteLater()
        QApplication.processEvents()
        self.directory.cleanup()

    def start(self):
        self.assertTrue(self.remote.set_enabled(True))
        spin_until(lambda: self.remote.status["state"] == "listening")

    def request(self, method, params=None, *, peer=PEER, request_id=None):
        return self.remote.handle_request(peer, {"id": request_id or str(uuid4()),
            "method": method, "params": params or {}})

    def pair(self, peer=PEER):
        pairing = self.remote.begin_pairing()
        response = self.request("pair", {"code": pairing["code"], "name": "测试手机"}, peer=peer)
        self.assertTrue(response["ok"], response)
        return pairing

    def test_default_is_off_without_identity_or_network_side_effects(self):
        self.assertFalse(self.remote.enabled)
        self.assertIsNone(self.remote._transport)
        self.assertEqual(self.vault.values, {})
        self.assertFalse((self.path / "remote").exists())
        self.assertEqual(self.request("status")["error"]["code"], "disabled")

    def test_pairing_is_single_use_and_expiry_is_checked_without_ui_timer(self):
        self.start()
        self.assertEqual(self.request("sync.pull")["error"]["code"], "unauthorized")
        self.assertEqual(self.request("pair", {"code": "wrong"})["error"]["code"], "pairing_failed")
        old = self.remote.begin_pairing()
        self.remote._clock = lambda: old["expires_at"] + 1
        self.assertEqual(self.request("pair", {"code": old["code"]})["error"]["code"], "pairing_failed")
        self.remote._clock = time.time
        pairing = self.pair()
        self.assertEqual(self.remote.pairing, {})
        self.assertEqual(self.request("pair", {"code": pairing["code"]}, peer=OTHER_PEER)["error"]["code"],
                         "pairing_failed")
        self.assertTrue(self.request("status")["ok"])

    def test_identity_is_stable_in_vault_and_never_written_to_preferences(self):
        self.start()
        secret = self.remote._transport.secret
        self.pair()
        self.assertNotIn(secret, (self.path / "remote" / "devices.json").read_text(encoding="utf-8"))
        self.assertEqual(RemotePreferences(self.settings).identity_secret(), secret)
        self.assertTrue(RemotePreferences(self.settings).authorized(PEER))

    def test_revocation_and_old_transport_callbacks_fail_after_restart(self):
        self.start()
        self.pair()
        old_callback = self.remote._transport.request_callback
        self.remote.retry()
        spin_until(lambda: self.remote.status["state"] == "listening")
        request = {"id": str(uuid4()), "method": "status", "params": {}}
        self.assertEqual(old_callback(PEER, request)["error"]["code"], "disabled")
        self.assertTrue(self.remote.revoke_device(PEER))
        self.assertEqual(self.request("sync.pull")["error"]["code"], "unauthorized")

    def test_pet_command_is_deduplicated_and_expires(self):
        self.start()
        self.pair()
        request_id = str(uuid4())
        params = {"action": "expression", "value": "happy", "expires_at": time.time() + 30}
        self.assertTrue(self.request("pet.command", params, request_id=request_id)["ok"])
        duplicate = self.request("pet.command", params, request_id=request_id)
        self.assertTrue(duplicate["result"]["duplicate"])
        self.assertEqual(self.pet.calls, [("expression", "happy")])
        self.assertEqual(self.request("pet.command", {**params, "expires_at": time.time() - 1})["error"]["code"],
                         "expired")

    def test_network_worker_dispatches_pet_operation_on_qt_thread(self):
        self.start()
        self.pair()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.request, "pet.command", {"action": "expression", "value": "surprised",
                                                                      "expires_at": time.time() + 30})
            spin_until(future.done)
            self.assertTrue(future.result()["ok"])
        self.assertEqual(self.pet.thread_ids, [threading.get_ident()])

    def test_image_upload_download_duplicate_and_delete(self):
        self.start()
        self.pair()
        params = image_params()
        first = self.request("image.upload", params)
        self.assertTrue(first["ok"], first)
        info = first["result"]
        self.assertEqual(info["filename"], "photo.png")
        self.assertEqual((info["width"], info["height"]), (24, 16))
        received = self.request("image.get", {"id": info["id"]})
        self.assertEqual(received["result"]["data_base64"], params["data_base64"])
        self.assertTrue(self.request("image.delete", {"id": info["id"]})["ok"])
        self.assertEqual(self.request("image.list")["result"]["items"], [])

    def test_upload_idempotency_does_not_store_image_data_in_operation_table(self):
        self.start()
        self.pair()
        request_id = str(uuid4())
        params = image_params()
        self.assertTrue(self.request("image.upload", params, request_id=request_id)["ok"])
        self.assertTrue(self.request("image.upload", params, request_id=request_id)["result"]["duplicate"])
        with self.chat.store._connection() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(remote_sync_operations)")]
            row = db.execute("SELECT * FROM remote_sync_operations WHERE request_id = ?", (request_id,)).fetchone()
        self.assertNotIn(params["data_base64"], json.dumps(dict(zip(columns, row))))

    def test_remote_chat_uses_one_ai_request_and_syncs_completed_reply(self):
        self.start()
        self.pair()
        with MockApiServer() as server:
            self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 10))
            self.settings.save_api_key("isolated-remote-test-key")
            server.enqueue('{"reply":"电脑已经收到手机消息。","emotion":"happy"}')
            request_id = str(uuid4())
            params = {"text": "手机发来的测试"}
            first = self.request("chat.send", params, request_id=request_id)
            self.assertTrue(first["ok"], first)
            self.assertTrue(self.request("chat.send", params, request_id=request_id)["result"]["duplicate"])
            spin_until(lambda: not self.chat.busy)
            cid = first["result"]["conversation_id"]
            messages = self.chat.store.load_messages(cid)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["id"], first["result"]["message_id"])
            snapshot = self.request("sync.snapshot")["result"]
            text = json.dumps(snapshot, ensure_ascii=False)
            self.assertIn("电脑已经收到手机消息。", text)
            self.assertNotIn("isolated-remote-test-key", text)
            self.assertEqual(len([request for request in server.requests
                                  if "手机发来的测试" in json.dumps(request["body"], ensure_ascii=False)]), 1)

    def test_remote_chat_does_not_switch_desktop_visible_conversation(self):
        self.start()
        self.pair()
        current = self.chat.current_conversation_id
        target = str(uuid4())
        with MockApiServer() as server:
            self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 10))
            self.settings.save_api_key("isolated-remote-test-key")
            server.enqueue('{"reply":"历史会话回复","emotion":"neutral"}')
            response = self.request("chat.send", {"text": "继续另一会话", "conversation_id": target})
            self.assertTrue(response["ok"], response)
            spin_until(lambda: not self.chat.busy)
            self.assertEqual(self.chat.current_conversation_id, current)
            self.assertEqual(self.chat.messages, [])
            self.assertEqual(len(self.chat.store.load_messages(target)), 2)

    def test_vault_failure_is_safe_and_starts_no_transport(self):
        self.vault.fail = True
        self.remote.set_enabled(True)
        self.assertEqual(self.remote.status["state"], "error")
        self.assertNotIn(self.vault.secret_for_exception, json.dumps(self.remote.status))
        self.assertIsNone(self.remote._transport)

    def test_malformed_saved_allowlist_fails_closed(self):
        directory = self.path / "remote"
        directory.mkdir()
        (directory / "devices.json").write_text('{"version":1,"enabled":"true","devices":[]}', encoding="utf-8")
        preferences = RemotePreferences(self.settings)
        self.assertFalse(preferences.enabled)
        self.assertEqual(preferences.devices, [])
        self.assertTrue(preferences.load_error)


if __name__ == "__main__":
    unittest.main()
