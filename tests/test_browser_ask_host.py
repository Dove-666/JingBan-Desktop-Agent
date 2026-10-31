"""Native Messaging framing and launch policy; no real app launch or secrets."""

from copy import deepcopy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from tools import browser_ask_host as host


MISSING_LISTENER = "桌宠尚未就绪，请启动 AURA 后再试。"


def message():
    return {"version": 1, "action": "ask_selection", "context": {
        "text": "HOST_SELECTED_PRIVATE_FIXTURE", "title": "当前合成网页",
        "url": "https://user:private@example.invalid/current?token=private#fragment",
        "page_text": "HOST_PAGE_PRIVATE_FIXTURE", "captured_at": "2026-10-31T09:00:00Z",
        "app_name": "Microsoft Edge"}}


def frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


class FragmentStream(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(2, size) if size >= 0 else 2)


class BrowserAskHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-browser-host-")
        self.config = Path(self.temp.name) / "isolated.ini"
        self.original_name = self.app.applicationName()

    def tearDown(self):
        self.app.setApplicationName(self.original_name)
        self.temp.cleanup()

    def decode(self, data):
        self.assertGreaterEqual(len(data), 4)
        length = struct.unpack("<I", data[:4])[0]
        self.assertEqual(len(data), length + 4, "stdout must contain exactly one framed JSON result")
        self.assertLessEqual(length, 4096)
        value = json.loads(data[4:].decode("utf-8"))
        self.assertIs(type(value["ok"]), bool)
        return value

    def test_fragmented_length_and_payload_are_read_and_unicode_response_roundtrips(self):
        value = message()
        self.assertEqual(host.read_message(FragmentStream(frame(value))), value)
        output = io.BytesIO()
        host.write_message(output, {"ok": False, "error": "合成中文错误"})
        self.assertEqual(self.decode(output.getvalue()), {"ok": False, "error": "合成中文错误"})

    def test_zero_oversized_truncated_invalid_utf8_and_json_frames_fail_without_page_echo(self):
        invalid = [b"", b"\x00\x00", struct.pack("<I", 0), struct.pack("<I", host.MAX_BYTES + 1),
                   struct.pack("<I", 100) + b"truncated", struct.pack("<I", 1) + b"\xff",
                   frame("HOST_PAGE_PRIVATE_FIXTURE")]
        for value in invalid:
            with self.subTest(value=value[:8]), self.assertRaises(host.NativeMessageError) as caught:
                host.read_message(io.BytesIO(value))
            self.assertNotIn("HOST_PAGE_PRIVATE_FIXTURE", str(caught.exception))

    def test_schema_rejects_wrong_action_bool_version_unknown_keys_and_missing_context(self):
        for mutate in (lambda value: value.update(action="execute"),
                       lambda value: value.update(version=True),
                       lambda value: value.update(extra="HOST_PAGE_PRIVATE_FIXTURE"),
                       lambda value: value.pop("context"),
                       lambda value: value.update(context=[])):
            value = message()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(host.NativeMessageError):
                host.read_message(io.BytesIO(frame(value)))

    def test_invalid_origin_is_denied_before_read_or_delivery_and_stdout_remains_framed_json(self):
        for arguments in ([], ["chrome-extension://different/"], [host.ALLOWED_ORIGIN + "spoof"],
                          [host.ALLOWED_ORIGIN, "--execute=calc.exe"]):
            output = io.BytesIO()
            source = Mock()
            source.read.side_effect = AssertionError("untrusted origin must not read input")
            with patch.object(host, "handle_message") as handle:
                code = host.main(["--config-path", str(self.config), *arguments],
                                 input_stream=source, output_stream=output)
            self.assertEqual(code, 1)
            self.assertFalse(self.decode(output.getvalue())["ok"])
            source.read.assert_not_called()
            handle.assert_not_called()

    def test_main_success_does_not_echo_page_and_accepts_browser_parent_window_argument(self):
        output = io.BytesIO()
        with patch.object(host, "handle_message", return_value={"ok": True}) as handle:
            code = host.main(["--config-path", str(self.config), host.ALLOWED_ORIGIN, "--parent-window=1234"],
                             input_stream=io.BytesIO(frame(message())), output_stream=output)
        self.assertEqual(code, 0)
        self.assertEqual(self.decode(output.getvalue()), {"ok": True})
        self.assertEqual(handle.call_args.args[1], self.config)
        self.assertNotIn("HOST_", output.getvalue().decode("utf-8", errors="ignore"))

    def test_main_decode_and_unexpected_failures_return_only_safe_json_errors(self):
        for malformed in (True, False):
            output = io.BytesIO()
            source = io.BytesIO(b"\x02\x00\x00\x00{x" if malformed else frame(message()))
            with patch.object(host, "handle_message", side_effect=RuntimeError("HOST_PAGE_PRIVATE_FIXTURE")):
                code = host.main(["--config-path", str(self.config), host.ALLOWED_ORIGIN],
                                 input_stream=source, output_stream=output)
            result = self.decode(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertNotIn("HOST_PAGE_PRIVATE_FIXTURE", json.dumps(result))
            self.assertNotIn("Traceback", output.getvalue().decode("utf-8", errors="ignore"))

    def test_existing_listener_success_has_no_launch_and_delivers_sanitized_page(self):
        deliver = Mock(return_value={"ok": True})
        launch = Mock(side_effect=AssertionError("must not launch existing pet"))
        value = message()
        before = deepcopy(value)
        result = host.handle_message(value, self.config, deliver=deliver, launch=launch)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(value, before, "input browser object must not be modified")
        sent = deliver.call_args.args[1]
        self.assertEqual(sent["action"], "ask_selection")
        self.assertTrue(sent["request_id"])
        self.assertEqual(sent["context"]["url"], "https://example.invalid/current")
        self.assertEqual(sent["context"]["text"], value["context"]["text"])
        launch.assert_not_called()
        self.assertNotIn("HOST_", json.dumps(result))

    def test_listener_rejection_never_launches_or_restarts_app(self):
        deliver = Mock(return_value={"ok": False, "error": "合成：提问框已达到上限"})
        launch = Mock()
        result = host.handle_message(message(), self.config, deliver=deliver, launch=launch)
        self.assertFalse(result["ok"])
        self.assertIn("上限", result["error"])
        self.assertEqual(deliver.call_count, 1)
        launch.assert_not_called()

    def test_absent_listener_launches_once_and_retries_same_request_identity(self):
        calls = []
        def deliver(config, value, **kwargs):
            calls.append(deepcopy(value))
            return {"ok": True} if len(calls) == 3 else {"ok": False, "error": MISSING_LISTENER}
        launch = Mock(return_value=True)
        result = host.handle_message(message(), self.config, deliver=deliver, launch=launch, timeout_seconds=.5)
        self.assertEqual(result, {"ok": True})
        launch.assert_called_once_with(self.config)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls, [calls[0]] * 3)

    def test_launch_failure_and_startup_deadline_are_safe_without_repeat_launch(self):
        for can_launch in (False, True):
            deliver = Mock(return_value={"ok": False, "error": MISSING_LISTENER})
            launch = Mock(return_value=can_launch)
            result = host.handle_message(message(), self.config, deliver=deliver, launch=launch, timeout_seconds=.1)
            self.assertFalse(result["ok"])
            launch.assert_called_once_with(self.config)
            self.assertNotIn("HOST_", json.dumps(result))

    def test_invalid_selection_never_delivers_or_launches_and_custom_config_never_spawns_real_app(self):
        value = message()
        value["context"]["text"] = []
        deliver, launch = Mock(), Mock()
        with self.assertRaises(ValueError):
            host.handle_message(value, self.config, deliver=deliver, launch=launch)
        deliver.assert_not_called()
        launch.assert_not_called()
        with patch.object(host, "default_config_path", return_value=self.config.parent / "other.ini"), \
                patch.object(host.subprocess, "Popen") as popen:
            self.assertFalse(host.start_pet(self.config))
            popen.assert_not_called()

    def test_oversized_error_response_is_replaced_with_small_json_frame(self):
        output = io.BytesIO()
        host.write_message(output, {"ok": False, "error": "HOST_PAGE_PRIVATE_FIXTURE" * 1000})
        result = self.decode(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertNotIn("HOST_PAGE_PRIVATE_FIXTURE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
