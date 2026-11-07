"""Only owned localhost fixtures; no production settings, keys or model calls."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from tools.qa_api_budget import BudgetError, BudgetLedger, BudgetProxy


class Upstream:
    def __init__(self):
        self.requests = []; self.lock = threading.Lock()
        self.first = threading.Event(); self.release = threading.Event(); self.disconnected = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw)
                with owner.lock:
                    owner.requests.append((self.path, self.headers.get("Authorization") == "Bearer FIXED_UPSTREAM_KEY", body))
                mode = body.get("mode")
                if mode == "drop":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                if mode == "redirect":
                    self.send_response(302); self.send_header("Location", owner.url+"/MUST_NOT_REQUEST")
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                if mode in {"error401", "error503"}:
                    raw = b'{"error":{"message":"FIXED_UPSTREAM_KEY PRIVATE_PROVIDER_ERROR_BODY"}}'
                    self.send_response(int(mode[5:])); self.send_header("Content-Type", "application/json")
                    self.send_header("X-Private-Provider", "FIXED_UPSTREAM_KEY")
                    self.send_header("Content-Length", str(len(raw))); self.end_headers()
                    try: self.wfile.write(raw)
                    except OSError: pass
                    return
                if mode in {"sse", "cancel"}:
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close"); self.end_headers()
                    self.wfile.write(b'data: {"delta":"FIRST"}\n\n'); self.wfile.flush(); owner.first.set()
                    if mode == "cancel":
                        self.connection.settimeout(4)
                        try:
                            if self.connection.recv(1) == b"": owner.disconnected.set()
                        except (OSError, socket.timeout): pass
                    else:
                        owner.release.wait(4)
                        try: self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
                        except OSError: pass
                    self.close_connection = True; return
                raw = b'{"ok":true}'
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=False)
        self.thread.start()

    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        if self.thread.is_alive(): raise AssertionError("Owned upstream listener did not stop")


class BudgetLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-budget-ledger-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/"budget.sqlite3"

    def ledger(self, limit=9, create=True):
        return BudgetLedger(self.path, session_id="fixed_budget_test", limit=limit, create=create)

    def test_threaded_reservations_are_shared_atomic_capped_and_never_refunded(self):
        ledger = self.ledger()
        def reserve(_):
            try: return self.ledger(create=False).reserve()
            except BudgetError as error:
                self.assertEqual("BUDGET_EXHAUSTED", error.code); return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(48)))
        self.assertEqual(list(range(1, 10)), sorted(row for row in results if row is not None))
        ledger.finish(1, "upstream_error")
        ledger.finish(2, "cancelled")
        self.assertEqual(9, self.ledger(create=False).count)
        with self.assertRaisesRegex(BudgetError, "BUDGET_EXHAUSTED"): ledger.reserve()

    def test_process_reservations_and_abrupt_exit_remain_durable(self):
        ledger = self.ledger(limit=5)
        code = """import os,sys\nfrom tools.qa_api_budget import BudgetLedger,BudgetError\nl=BudgetLedger(sys.argv[1],session_id='fixed_budget_test',limit=5)\nfor _ in range(4):\n try:l.reserve()\n except BudgetError:pass\nos._exit(0)\n"""
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.path)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if sys.platform == "win32" else 0) for _ in range(3)]
        try:
            for process in processes: self.assertEqual(0, process.wait(timeout=10))
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait(timeout=5)
        self.assertEqual(5, ledger.count)
        self.assertEqual({"reserved": 5}, ledger.snapshot()["outcomes"])

    def test_missing_replaced_or_wrong_identity_ledger_never_reinitializes(self):
        ledger = self.ledger(); ledger.reserve()
        for kwargs in ({"session_id":"different", "limit":9}, {"session_id":"fixed_budget_test", "limit":10}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BudgetError): BudgetLedger(self.path, **kwargs)
        self.path.unlink()
        with self.assertRaises(BudgetError): self.ledger(create=False)
        self.assertFalse(self.path.exists())
        with self.assertRaises(BudgetError): ledger.reserve()


class BudgetProxyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-budget-proxy-")
        self.addCleanup(self.directory.cleanup)
        self.upstream = Upstream(); self.addCleanup(self.upstream.close)
        self.path = Path(self.directory.name)/"budget.sqlite3"

    def ledger(self, limit=100, create=True):
        return BudgetLedger(self.path, session_id="fixed_proxy_test", limit=limit, create=create)

    def proxy(self, ledger=None):
        return BudgetProxy(ledger or self.ledger(), self.upstream.url+"/v1", "FIXED_UPSTREAM_KEY",
                           timeout_seconds=5, trust_env=False)

    def post(self, proxy, body, **kwargs):
        return httpx.post(proxy.base_url+"/chat/completions", json=body,
            headers={"Authorization":"Bearer "+proxy.token}, timeout=6, trust_env=False, **kwargs)

    def test_proxy_caps_actual_requests_persists_between_instances_and_keeps_secrets_out_of_db(self):
        ledger = self.ledger(limit=2)
        with self.proxy(ledger) as proxy:
            self.assertEqual({"ok":True}, self.post(proxy, {"nonce":"BODY_MUST_NOT_BE_SAVED"}).json())
            self.assertEqual(1, ledger.count)
        self.assertTrue(proxy.snapshot()["closed"])
        with self.proxy(self.ledger(limit=2, create=False)) as second:
            self.assertEqual(200, self.post(second, {}).status_code)
            self.assertEqual(429, self.post(second, {}).status_code)
        self.assertEqual(2, len(self.upstream.requests))
        self.assertTrue(all(path=="/v1/chat/completions" and auth for path,auth,_ in self.upstream.requests))
        database = self.path.read_bytes()
        for private in (b"FIXED_UPSTREAM_KEY", b"BODY_MUST_NOT_BE_SAVED", proxy.token.encode(), second.token.encode()):
            self.assertNotIn(private, database)

    def test_concurrent_http_requests_never_forward_beyond_the_same_durable_cap(self):
        ledger = self.ledger(limit=4)
        with self.proxy(ledger) as proxy:
            with ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(pool.map(lambda _:self.post(proxy, {}).status_code, range(12)))
            self.assertEqual(4, statuses.count(200))
            self.assertEqual(8, statuses.count(429))
        self.assertEqual(4, ledger.count)
        self.assertEqual(4, len(self.upstream.requests))

    def test_bad_auth_wrong_endpoint_bad_json_or_forwarding_path_cost_nothing(self):
        with self.proxy() as proxy:
            endpoint = proxy.base_url+"/chat/completions"
            response = httpx.post(endpoint, json={}, headers={"Authorization":"Bearer WRONG"}, trust_env=False)
            self.assertEqual(401, response.status_code)
            response = httpx.get(proxy.base_url+"/models", trust_env=False)
            self.assertEqual(405, response.status_code)
            response = httpx.post(proxy.base_url+"/chat/completions?target=elsewhere", json={},
                headers={"Authorization":"Bearer "+proxy.token}, trust_env=False)
            self.assertEqual(405, response.status_code)
            response = httpx.post(endpoint, content=b'{"duplicate":1,"duplicate":2}',
                headers={"Authorization":"Bearer "+proxy.token,"Content-Type":"application/json"}, trust_env=False)
            self.assertEqual(400, response.status_code)
            self.assertEqual(0, proxy.ledger.count)
        self.assertEqual([], self.upstream.requests)

    def test_upstream_failure_and_redirect_each_spend_one_without_retry_or_follow(self):
        with self.proxy() as proxy:
            for mode in ("drop", "redirect"):
                with self.subTest(mode=mode): self.assertEqual(502, self.post(proxy, {"mode":mode}).status_code)
            self.assertEqual(2, proxy.ledger.count)
        self.assertEqual({"upstream_error":1,"redirect_refused":1}, proxy.ledger.snapshot()["outcomes"])
        self.assertEqual(2, len(self.upstream.requests))

    def test_non_success_error_status_survives_but_provider_key_body_and_headers_do_not(self):
        with self.proxy() as proxy:
            for status in (401, 503):
                with self.subTest(status=status):
                    response = self.post(proxy, {"mode":"error"+str(status)})
                    self.assertEqual(status, response.status_code)
                    self.assertEqual({"error":{"message":f"BUDGET_UPSTREAM_HTTP_{status}",
                        "type":"aura_qa_budget","code":f"BUDGET_UPSTREAM_HTTP_{status}"}}, response.json())
                    self.assertNotIn("FIXED_UPSTREAM_KEY", response.text)
                    self.assertNotIn("PRIVATE_PROVIDER_ERROR_BODY", response.text)
                    self.assertNotIn("x-private-provider", response.headers)
            self.assertEqual(2, proxy.ledger.count)
        self.assertEqual(2, len(self.upstream.requests))
        self.assertEqual({"upstream_error":2}, proxy.ledger.snapshot()["outcomes"])

    def test_sse_prefix_is_forwarded_before_completion_and_clean_end_preserved(self):
        with self.proxy() as proxy, httpx.Client(trust_env=False, timeout=6) as client:
            with client.stream("POST", proxy.base_url+"/chat/completions", json={"mode":"sse"},
                    headers={"Authorization":"Bearer "+proxy.token}) as response:
                self.assertEqual(200, response.status_code)
                chunks = response.iter_bytes(); first = next(chunks)
                self.assertIn(b'FIRST', first)
                self.assertFalse(self.upstream.release.is_set())
                self.upstream.release.set()
                self.assertIn(b'[DONE]', b''.join(chunks))
            self.assertEqual(1, proxy.ledger.count)

    def test_downstream_cancel_closes_exact_upstream_without_a_second_request(self):
        with self.proxy() as proxy, httpx.Client(trust_env=False, timeout=6) as client:
            with client.stream("POST", proxy.base_url+"/chat/completions", json={"mode":"cancel"},
                    headers={"Authorization":"Bearer "+proxy.token}) as response:
                self.assertIn(b'FIRST', next(response.iter_bytes()))
            self.assertTrue(self.upstream.disconnected.wait(3), "Exact upstream connection must close on downstream cancellation")
            deadline = time.monotonic()+3
            while proxy.ledger.snapshot()["outcomes"].get("cancelled") != 1 and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual({"cancelled":1}, proxy.ledger.snapshot()["outcomes"])
        self.assertEqual(1, len(self.upstream.requests))

    def test_proxy_close_with_an_active_sse_reaps_handlers_and_closes_upstream(self):
        proxy = self.proxy().__enter__()
        client = httpx.Client(trust_env=False, timeout=6)
        response = client.send(client.build_request("POST", proxy.base_url+"/chat/completions", json={"mode":"cancel"},
            headers={"Authorization":"Bearer "+proxy.token}), stream=True)
        try:
            self.assertIn(b'FIRST', next(response.iter_bytes()))
            proxy.close()
            self.assertTrue(self.upstream.disconnected.wait(3))
            self.assertTrue(proxy.snapshot()["closed"])
            self.assertFalse(proxy.snapshot()["running"])
        finally:
            response.close(); client.close()
            if proxy.snapshot()["running"]: proxy.close()

    def test_repeated_close_and_disconnect_drain_the_owned_metadata_finalizer(self):
        ledger = self.ledger()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        actual = ledger.finish
        failures = []
        def held(*args):
            entered.set()
            if not release.wait(3): raise AssertionError("Owned ledger finalizer release missing")
            actual(*args)
        proxy = self.proxy(ledger).__enter__()
        def close():
            try: proxy.close()
            except BaseException as error: failures.append(type(error).__name__)
            finally: finished.set()
        closer = None
        try:
            with patch.object(ledger, "finish", side_effect=held):
                self.assertEqual(200, self.post(proxy, {}).status_code)
                self.assertTrue(entered.wait(2))
                closer = threading.Thread(target=close, daemon=False); closer.start()
                self.assertFalse(finished.wait(.05), "Closure cannot precede the exact pending metadata write")
                release.set(); closer.join(4)
                self.assertFalse(closer.is_alive()); self.assertEqual([], failures)
            self.assertTrue(proxy.snapshot()["closed"])
            self.assertEqual({"completed":1}, ledger.snapshot()["outcomes"])
        finally:
            release.set()
            if closer is not None: closer.join(4)
            if proxy.snapshot()["running"]: proxy.close()


if __name__ == "__main__": unittest.main()
