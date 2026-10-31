"""Bounded framing and optional real iroh endpoint round trips.

Network tests are opt-in, including LAN/loopback endpoints, because n0 discovery
and public relay connectivity can access the internet:
  $env:AURA_REMOTE_NETWORK_TESTS='1'
  .venv\Scripts\python -m unittest tests.test_remote_transport -v

The smoke reports the SDK's selected path. Two endpoints on one computer usually
switch to a direct path; a configured public relay does not prove relay traffic.
"""

import asyncio
import base64
from concurrent.futures import Future
import importlib.util
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.remote.protocol import (MAX_FRAME_BYTES, UNPAIRED_MAX_FRAME_BYTES,
                                        ProtocolError, decode_payload,
                                        encode_frame, read_frame, validate_request)
from desktop_pet.remote.transport import IrohPeerClient, IrohTransport, TransportError


class _Reader:
    def __init__(self, data):
        self.data = data
        self.read_sizes = []

    async def read_exact(self, size):
        self.read_sizes.append(size)
        part, self.data = self.data[:size], self.data[size:]
        return part


class ProtocolTests(unittest.TestCase):
    def test_unicode_frame_round_trip(self):
        value = {"id": str(uuid4()), "method": "chat.send", "params": {"text": "你好 🌊"}}
        self.assertEqual(asyncio.run(read_frame(_Reader(encode_frame(value)))), value)

    def test_length_limit_is_checked_before_payload_read(self):
        for size in (0, MAX_FRAME_BYTES + 1):
            reader = _Reader(struct.pack(">I", size))
            with self.assertRaises(ProtocolError):
                asyncio.run(read_frame(reader))
            self.assertEqual(reader.read_sizes, [4])

    def test_unpaired_limit_rejects_before_body_and_maximum_image_fits(self):
        reader = _Reader(struct.pack(">I", UNPAIRED_MAX_FRAME_BYTES + 1))
        with self.assertRaises(ProtocolError) as error:
            asyncio.run(read_frame(reader, max_frame_bytes=UNPAIRED_MAX_FRAME_BYTES))
        self.assertEqual(error.exception.code, "frame_too_large")
        self.assertEqual(reader.read_sizes, [4])
        maximum_image = base64.b64encode(b"\x00" * (4 * 1024 * 1024)).decode("ascii")
        encoded = encode_frame({"id": str(uuid4()), "method": "image.upload",
                                "params": {"filename": "image.png", "data_base64": maximum_image}})
        self.assertLess(len(encoded), MAX_FRAME_BYTES)

    def test_oversized_response_has_explicit_frame_error(self):
        with self.assertRaises(ProtocolError) as error:
            encode_frame({"content": "x" * MAX_FRAME_BYTES})
        self.assertEqual(error.exception.code, "frame_too_large")

    def test_duplicate_fields_and_non_finite_numbers_are_rejected(self):
        for payload in (b'{"id":1,"id":2}', b'{"x":NaN}', b'[]', b'\xff'):
            with self.assertRaises(ProtocolError):
                decode_payload(payload)

    def test_request_identity_cannot_be_injected(self):
        valid = {"id": str(uuid4()), "method": "status", "params": {}}
        self.assertEqual(validate_request(valid), valid)
        for value in ({**valid, "peer_id": "forged"}, {**valid, "id": "bad"},
                      {**valid, "params": []}, {**valid, "method": "../shell"}):
            with self.assertRaises(ProtocolError):
                validate_request(value)

    def test_missing_dependency_fails_without_plaintext_fallback(self):
        states = []
        event = threading.Event()
        def status(value):
            states.append(value)
            if value["state"] == "error":
                event.set()
        with patch("desktop_pet.remote.transport.importlib.import_module", side_effect=ImportError("private detail")):
            transport = IrohTransport(secrets.token_hex(32), lambda *_: {}, status)
            transport.start()
            self.assertTrue(event.wait(3))
        self.assertEqual(states[-1]["state"], "error")
        self.assertNotIn("private detail", states[-1]["error"])
        self.assertEqual(states[-1]["ticket"], "")

    def test_stop_interrupts_startup_before_bind_completes(self):
        entered = threading.Event()
        states = []
        async def pending_bind(*_):
            entered.set()
            await asyncio.Event().wait()
        with patch("desktop_pet.remote.transport._load_iroh", return_value=object()), \
             patch("desktop_pet.remote.transport._bind_endpoint", side_effect=pending_bind):
            transport = IrohTransport(secrets.token_hex(32), lambda *_: {}, states.append)
            transport.start()
            self.assertTrue(entered.wait(2))
            transport.stop()
            self.assertTrue(transport.wait_stopped(2))
        self.assertEqual(states[-1]["state"], "stopped")


@unittest.skipUnless(os.environ.get("AURA_REMOTE_NETWORK_TESTS") == "1"
                     and importlib.util.find_spec("iroh") is not None,
                     "Opt-in real iroh network test")
class RealIrohTests(unittest.TestCase):
    def setUp(self):
        self.listening = threading.Event()
        self.stopped = threading.Event()
        self.states = []
        self.identities = []
        def status(value):
            self.states.append(value)
            if value["state"] in {"listening", "error"}:
                self.listening.set()
            if value["state"] == "stopped":
                self.stopped.set()
        def dispatch(peer_id, value):
            self.identities.append(peer_id)
            return {"id": value["id"], "ok": True,
                    "result": {"method": value["method"], "value": value["params"].get("value")}}
        self.transport = IrohTransport(secrets.token_hex(32), dispatch, status)
        self.addCleanup(self.transport.stop)
        self.transport.start()
        self.assertTrue(self.listening.wait(35), "Endpoint did not finish starting")
        self.assertEqual(self.states[-1]["state"], "listening", self.states[-1]["error"])

    def tearDown(self):
        self.transport.stop()
        self.assertTrue(self.stopped.wait(10), "Endpoint did not stop")
        self.assertTrue(self.transport.wait_stopped(2), "Transport thread did not exit")
        self.assertFalse([thread for thread in threading.enumerate()
                          if thread.name.startswith("aura-remote-request")],
                         "Remote worker threads remained after shutdown")

    def test_real_encrypted_round_trip_and_authenticated_identity(self):
        secret = secrets.token_hex(32)
        async def smoke():
            async with IrohPeerClient(secret) as client:
                await client.connect(self.states[-1]["ticket"])
                responses = await asyncio.gather(*[
                    client.request({"id": str(uuid4()), "method": "status",
                                    "params": {"value": number}}) for number in range(4)])
                for number, response in enumerate(responses):
                    self.assertTrue(response["ok"])
                    self.assertEqual(response["result"]["value"], number)
                self.assertTrue(all(peer == client.endpoint_id for peer in self.identities))
                self.assertTrue(client.public_relay_configured)
                print(json.dumps({"real_quic_round_trip": True,
                                  "public_relay_configured": client.public_relay_configured,
                                  "observed_connection_type": client.connection_type,
                                  "public_relay_path_observed": client.connection_type == "relay"}))
                old_endpoint = client._endpoint
                await client.connect(self.states[-1]["ticket"])
                self.assertTrue(old_endpoint.is_closed(), "Reconnect retained the previous native endpoint")
                self.assertTrue((await client.request(
                    {"id": str(uuid4()), "method": "status", "params": {}}))["ok"])
        asyncio.run(smoke())

    def test_unpaired_large_frame_is_rejected_without_dispatch_or_deadlock(self):
        allowed = {"value": False}
        self.transport._is_authorized = lambda _: allowed["value"]
        async def smoke():
            async with IrohPeerClient() as client:
                await client.connect(self.states[-1]["ticket"])
                large = {"id": str(uuid4()), "method": "image.upload",
                         "params": {"value": "x" * (1024 * 1024)}}
                with self.assertRaises(ProtocolError) as error:
                    await client.request(large)
                self.assertEqual(error.exception.code, "frame_too_large")
                self.assertEqual(self.identities, [])
                allowed["value"] = True
                large["id"] = str(uuid4())
                self.assertTrue((await client.request(large))["ok"])
        asyncio.run(smoke())

    def test_cancelled_ui_wait_stops_workers_and_same_identity_can_restart(self):
        # The real Qt controller cancels these futures before transport.stop().
        waiting_ui = Future()
        entered = threading.Event()
        original_dispatch = self.transport._on_request
        def dispatch(peer_id, value):
            entered.set()
            waiting_ui.result(timeout=5)
            return original_dispatch(peer_id, value)
        self.transport._on_request = dispatch
        previous_id = self.states[-1]["endpoint_id"]
        async def smoke():
            async with IrohPeerClient() as client:
                await client.connect(self.states[-1]["ticket"])
                pending = asyncio.create_task(client.request(
                    {"id": str(uuid4()), "method": "status", "params": {}}))
                deadline = time.monotonic() + 5
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                waiting_ui.cancel()
                started = time.monotonic()
                self.transport.stop()
                # stop() only schedules cancellation; no network join on UI.
                self.assertLess(time.monotonic() - started, 0.1)
                await asyncio.gather(pending, return_exceptions=True)
        asyncio.run(smoke())
        self.assertTrue(self.transport.wait_stopped(3))
        self.assertFalse([thread for thread in threading.enumerate()
                          if thread.name.startswith("aura-remote-request")])
        self.transport._on_request = original_dispatch
        self.listening.clear()
        self.stopped.clear()
        self.transport.start()
        self.assertTrue(self.listening.wait(35))
        self.assertEqual(self.states[-1]["state"], "listening")
        self.assertEqual(self.states[-1]["endpoint_id"], previous_id)
        async def after_restart():
            async with IrohPeerClient() as client:
                await client.connect(self.states[-1]["ticket"])
                self.assertTrue((await client.request(
                    {"id": str(uuid4()), "method": "status", "params": {}}))["ok"])
        asyncio.run(after_restart())

    def test_development_cli_reads_private_files_without_printing_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticket_file = root / "ticket.txt"
            identity_file = root / "identity.key"
            request_file = root / "request.json"
            output_file = root / "response.json"
            ticket_file.write_text(self.states[-1]["ticket"], encoding="utf-8")
            private_value = "private-chat-sentinel"
            request_file.write_text(json.dumps({"id": str(uuid4()), "method": "chat.send",
                                                "params": {"value": private_value}}), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "tools" / "remote_peer.py"),
                 "--ticket-file", str(ticket_file), "--identity-file", str(identity_file),
                 "--request-file", str(request_file), "--output-file", str(output_file)],
                capture_output=True, text=True, timeout=40)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn(private_value, completed.stdout + completed.stderr)
            self.assertNotIn(identity_file.read_text(), completed.stdout + completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["ok"])
            self.assertEqual(json.loads(output_file.read_text())["result"]["value"], private_value)


if __name__ == "__main__":
    unittest.main()
