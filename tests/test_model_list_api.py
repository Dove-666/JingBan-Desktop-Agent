"""Real local HTTP model discovery; no provider calls or stored credentials."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import socket
import threading
import time
import traceback
import unittest

from desktop_pet.api_client import ApiClient, ApiError, MAX_MODEL_CHOICES
from desktop_pet.settings_manager import ApiSettings, SettingsError


KEY = "models-local-fixture-token"


class ModelApiServer:
    """Reusable GET fixture: enqueue(body, status=200, delay=0), requests."""

    def __init__(self):
        self.requests = []
        self.received = threading.Event()
        self.plans = Queue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append({"method": "GET", "path": self.path,
                                       "authorization": self.headers.get("Authorization")})
                owner.received.set()
                plan = owner.plans.get(timeout=2)
                time.sleep(plan["delay"])
                payload = (plan["body"].encode("utf-8") if isinstance(plan["body"], str)
                           else json.dumps(plan["body"]).encode("utf-8"))
                try:
                    self.send_response(plan["status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def do_POST(self):
                owner.requests.append({"method": "POST", "path": self.path})
                self.send_error(405)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)

    def enqueue(self, body=None, *, status=200, delay=0):
        if body is None:
            body = {"object": "list", "data": [{"id": "test-chat", "object": "model"}]}
        self.plans.put({"body": body, "status": status, "delay": delay})

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def model_body(*names):
    return {"object": "list", "data": [{"id": name, "object": "model"} for name in names]}


class ModelListApiTests(unittest.TestCase):
    def test_real_get_uses_base_path_auth_without_requiring_model_or_chat(self):
        with ModelApiServer() as server:
            server.enqueue(model_body("z-chat", "a-chat", "z-chat", " custom/vision ", "A-chat"))
            settings = ApiSettings(server.base_url + "/gateway/", "")
            result = ApiClient().list_models(settings, KEY)
            self.assertEqual(result, ["A-chat", "a-chat", "custom/vision", "z-chat"])
            self.assertEqual(server.requests, [{"method": "GET", "path": "/v1/gateway/models",
                                                "authorization": "Bearer " + KEY}])
            self.assertEqual(settings.model, "")

    def test_empty_response_explains_manual_entry(self):
        with ModelApiServer() as server:
            server.enqueue(model_body())
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url), KEY)
            self.assertEqual(caught.exception.code, "empty_models")
            self.assertIn("手动", str(caught.exception))

    def test_invalid_payloads_do_not_report_success(self):
        with ModelApiServer() as server:
            for body in ({"data": None}, {"data": "not-list"}, {}, "<html>not-json</html>",
                         {"data": [{"no_id": "missing"}, {"id": 42}, {"id": " "}]}):
                with self.subTest(body=body):
                    server.enqueue(body)
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().list_models(ApiSettings(server.base_url), KEY)
                    self.assertEqual(caught.exception.code, "invalid_response")

    def test_secret_and_control_character_ids_never_cross_transport_boundary(self):
        with ModelApiServer() as server:
            server.enqueue(model_body("safe/模型", KEY, "prefix-" + KEY, "\nbad", "bad\tname", "bad\x7fname"))
            self.assertEqual(ApiClient().list_models(ApiSettings(server.base_url), KEY), ["safe/模型"])
            server.enqueue(model_body(KEY))
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url), KEY)
            self.assertNotIn(KEY, str(caught.exception))

    def test_single_page_does_not_follow_untrusted_next_links_or_claim_completeness(self):
        with ModelApiServer() as server:
            body = model_body("first")
            body.update({"has_more": True, "next": "https://other.example/steal"})
            server.enqueue(body)
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url), KEY)
            self.assertEqual(caught.exception.code, "models_incomplete")
            self.assertEqual(len(server.requests), 1)

    def test_list_size_is_bounded_without_silent_truncation(self):
        with ModelApiServer() as server:
            server.enqueue(model_body(*(f"model-{index}" for index in range(MAX_MODEL_CHOICES + 1))))
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url), KEY)
            self.assertEqual(caught.exception.code, "models_incomplete")

    def test_http_errors_are_static_sanitized_and_never_retried(self):
        cases = ((401, "authentication"), (403, "permission"), (404, "models_unsupported"),
                 (405, "models_unsupported"), (501, "models_unsupported"),
                 (429, "rate_limit"), (400, "provider"), (500, "provider"))
        with ModelApiServer() as server:
            for index, (status, code) in enumerate(cases):
                with self.subTest(status=status):
                    server.enqueue({"error": {"message": "server reflected " + KEY}}, status=status)
                    try:
                        ApiClient().list_models(ApiSettings(server.base_url), KEY)
                    except ApiError as exc:
                        self.assertEqual(exc.code, code)
                        self.assertNotIn(KEY, str(exc))
                        self.assertNotIn(KEY, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                    else:
                        self.fail("HTTP error reported as success")
                    self.assertEqual(len(server.requests), index + 1)

    def test_timeout_bounds_whole_request_and_never_retries(self):
        with ModelApiServer() as server:
            server.enqueue(delay=2)
            started = time.monotonic()
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url, timeout_seconds=1), KEY)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertLess(time.monotonic() - started, 1.8)
            self.assertEqual(len(server.requests), 1)

    def test_invalid_configuration_or_missing_key_never_sends_request(self):
        with ModelApiServer() as server:
            for settings in (ApiSettings("not-a-url"), ApiSettings(server.base_url, timeout_seconds=0)):
                with self.assertRaises(SettingsError):
                    ApiClient().list_models(settings, KEY)
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url), " ")
            self.assertEqual(caught.exception.code, "not_configured")
            self.assertEqual(server.requests, [])

    def test_network_failure_is_readable_and_sanitized(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(ApiError) as caught:
            ApiClient().list_models(ApiSettings(f"http://127.0.0.1:{port}/v1"), KEY)
        self.assertEqual(caught.exception.code, "network")
        self.assertNotIn(KEY, str(caught.exception))

    def test_pre_cancel_and_shutdown_prevent_http(self):
        with ModelApiServer() as server:
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(ApiError) as caught:
                ApiClient().list_models(ApiSettings(server.base_url), KEY, cancellation_event=cancelled)
            self.assertEqual(caught.exception.code, "cancelled")
            client = ApiClient()
            client.shutdown()
            with self.assertRaises(ApiError) as caught:
                client.list_models(ApiSettings(server.base_url), KEY)
            self.assertEqual(caught.exception.code, "cancelled")
            self.assertEqual(server.requests, [])

    def test_cancelling_one_request_cleans_task_and_allows_later_discovery(self):
        with ModelApiServer() as server:
            server.enqueue(delay=2)
            client, cancelled, results = ApiClient(), threading.Event(), Queue()

            def request():
                try:
                    results.put(client.list_models(ApiSettings(server.base_url), KEY, cancellation_event=cancelled))
                except ApiError as exc:
                    results.put(exc)

            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            self.assertTrue(server.received.wait(timeout=3))
            client.cancel_request(cancelled)
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results.get_nowait().code, "cancelled")
            self.assertEqual(client._tasks, set())
            self.assertEqual(client._request_cancellations, {})
            server.enqueue(model_body("after-cancel"))
            self.assertEqual(client.list_models(ApiSettings(server.base_url), KEY), ["after-cancel"])


if __name__ == "__main__":
    unittest.main()
