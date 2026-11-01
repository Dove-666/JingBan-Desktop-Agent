"""Opt-in encrypted second client against the complete desktop remote service.

Set AURA_REMOTE_NETWORK_TESTS=1 to exercise real iroh endpoints. The client
and host run on one computer: selected direct paths are not cross-network
or public-relay evidence. No real account, chat API or user settings are used.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from uuid import uuid4

from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.remote.controller import RemoteController
from desktop_pet.remote.transport import IrohPeerClient
from desktop_pet.settings_manager import SettingsManager, CredentialStore, ApiSettings
from tests.mock_api import MockApiServer
from tests.test_remote_controller import PetStub, image_params, spin_until
from tests.test_settings import FakeVault


@unittest.skipUnless(os.environ.get("AURA_REMOTE_NETWORK_TESTS") == "1"
                     and importlib.util.find_spec("iroh") is not None,
                     "Opt-in real encrypted remote integration")
class RemoteEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pair_chat_image_commands_sync_and_live_revoke(self):
        temp = tempfile.TemporaryDirectory(prefix="aura-remote-e2e-")
        path = Path(temp.name)
        vault = FakeVault()
        settings = SettingsManager(QSettings(str(path / "settings.ini"), QSettings.Format.IniFormat),
                                   CredentialStore(backend=vault))
        chat = ChatController(settings)
        pet = PetStub()
        session = SimpleNamespace(settings=settings, chat=chat, pet_controller=pet,
                                  pet=SimpleNamespace(setVisible=lambda value: None),
                                  tray=SimpleNamespace(available=True, refresh=lambda: None))
        remote = RemoteController(session)
        report = {"cross_network_tested": False, "phone_app_tested": False,
                  "public_relay_path_observed": False}
        try:
            with MockApiServer() as api:
                settings.save_api_settings(ApiSettings(api.base_url, "test-model", 10))
                settings.save_api_key("isolated-encrypted-e2e-key")
                api.enqueue('{"reply":"加密通信端到端验证完成。","emotion":"happy"}')
                remote.set_enabled(True)
                spin_until(lambda: remote.status["state"] in ("listening", "error"), timeout=40)
                self.assertEqual(remote.status["state"], "listening", remote.status["message"])
                pairing = remote.begin_pairing()
                ticket = remote.status["ticket"]
                upload = image_params()
                self_id = []
                async def client_flow():
                    async with IrohPeerClient() as client:
                        start = time.perf_counter()
                        await client.connect(ticket)
                        report["connection_ms"] = round((time.perf_counter() - start) * 1000, 2)
                        self_id.append(client.endpoint_id)
                        async def call(method, params=None, request_id=None):
                            return await client.request({"id": request_id or str(uuid4()),
                                "method": method, "params": params or {}})
                        denied = await call("status")
                        self.assertEqual(denied["error"]["code"], "unauthorized")
                        self.assertTrue((await call("pair", {"code": pairing["code"], "name": "真实开发客户端"}))["ok"])
                        start = time.perf_counter()
                        self.assertTrue((await call("status"))["ok"])
                        report["local_status_rtt_ms"] = round((time.perf_counter() - start) * 1000, 2)
                        self.assertTrue((await call("pet.command", {"action": "expression", "value": "happy",
                            "expires_at": time.time() + 30}))["ok"])
                        image = await call("image.upload", upload)
                        self.assertTrue(image["ok"], image)
                        downloaded = await call("image.get", {"id": image["result"]["id"]})
                        self.assertEqual(downloaded["result"]["data_base64"], upload["data_base64"])
                        request_id = str(uuid4())
                        sent = await call("chat.send", {"text": "从第二个真实加密客户端发来"}, request_id)
                        self.assertTrue(sent["ok"], sent)
                        again = await call("chat.send", {"text": "从第二个真实加密客户端发来"}, request_id)
                        self.assertTrue(again["result"]["duplicate"])
                        for _ in range(60):
                            status = await call("status")
                            if not status["result"]["chat_busy"]:
                                break
                            await asyncio.sleep(.1)
                        snapshot = await call("sync.snapshot")
                        self.assertTrue(snapshot["ok"], snapshot)
                        self.assertIn("加密通信端到端验证完成。", json.dumps(snapshot, ensure_ascii=False))
                        self.assertNotIn("isolated-encrypted-e2e-key", json.dumps(snapshot))
                        pull = await call("sync.pull", {"since": snapshot["result"]["watermark"]})
                        self.assertTrue(pull["ok"], pull)
                        report["selected_path"] = client.connection_type
                        report["public_relay_configured"] = client.public_relay_configured
                        report["public_relay_path_observed"] = client.connection_type == "relay"
                        report["checks"] = ["unpaired_denied", "pair", "authenticated_status", "pet_command",
                                            "image_roundtrip", "chat_and_reply", "duplicate_no_second_ai", "snapshot", "incremental_pull"]
                        # Exercise revocation while the encrypted connection is still open.
                        await asyncio.to_thread(remote._call_ui, lambda: remote.revoke_device(client.endpoint_id))
                        self.assertEqual((await call("status"))["error"]["code"], "unauthorized")
                        report["checks"].append("live_connection_revocation")
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(asyncio.run, client_flow())
                    spin_until(future.done, timeout=70)
                    future.result()
                self.assertIn(("expression", "happy"), pet.calls)
                messages = chat.store.load_messages(chat.current_conversation_id)
                self.assertEqual(len(messages), 2)
                report["message_count"] = len(messages)
                report["passed"] = True
                artifact = Path(__file__).resolve().parents[1] / "artifacts" / "remote_connection" / "integration.json"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            transport = remote._transport
            remote.shutdown()
            chat.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            if transport is not None:
                spin_until(lambda: transport._thread is None or not transport._thread.is_alive(), timeout=10)
            remote.deleteLater()
            chat.deleteLater()
            QApplication.processEvents()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
