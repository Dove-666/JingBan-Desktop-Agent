"""Mock transport only: no native operator, screenshot, or desktop input."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_bridge import GuiBridge, BridgeFailure, authorized_gui_callback
from gui_verification import report


class FakeInput:
    def __init__(self):
        self.lines = []
    def write(self, value):
        self.lines.append(json.loads(value))
    async def drain(self):
        pass
    def close(self):
        pass


class FakeProcess:
    def __init__(self, completed=True):
        self.stdin = FakeInput()
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.pid = 99999999
        self.stdout.feed_data(b'{"type":"ready"}\n')
        if completed:
            self.stdout.feed_data(b'{"type":"progress","id":"gui-1","stage":"acting"}\n')
            self.stdout.feed_data(b'{"type":"final","id":"gui-1","status":"needs_user","summary":"test-key","steps":2}\n')
    async def wait(self):
        self.returncode = 0
        return 0


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self, **kwargs):
        return GuiBridge({"base_url": "http://127.0.0.1:9/v1", "api_key": "test-key", "model": "fake"},
                         "gui-1", {"max_steps": 4, "timeout_seconds": 120}, lambda *_args, **_kw: None, **kwargs)

    async def test_json_transport_credentials_status_and_single_dispatch(self):
        process = FakeProcess()
        captured = {}
        calls = []
        async def spawn(*argv, **kwargs):
            calls.append(argv)
            captured.update(argv=argv, kwargs=kwargs)
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), patch("gui_bridge.shutil.which", return_value=sys.executable):
            bridge = self.make_bridge()
            bridge.reference_context = "reference " + "x" * 150000 + " test-key"
            execute = authorized_gui_callback(bridge, "original user desktop goal")
            result = json.loads(await execute(task="model-rewritten unauthorized goal"))
            self.assertEqual(result["status"], "needs_user")
            self.assertNotIn("test-key", result["summary"])
            self.assertNotIn("test-key", str(captured["argv"]))
            self.assertEqual(process.stdin.lines[0]["config"]["apiKey"], "test-key")
            self.assertEqual(process.stdin.lines[0]["task"], "original user desktop goal")
            self.assertEqual(process.stdin.lines[0]["options"]["timeoutMs"], 120000)
            self.assertEqual(process.stdin.lines[0]["reference_context"], "reference " + "x" * 150000 + " [redacted]")
            self.assertEqual(process.stdin.lines[-1]["type"], "abort")
            repeated = json.loads(await execute(task="another model-authored goal"))
            self.assertEqual(repeated, result)
            self.assertEqual(len(calls), 1)
            self.assertFalse(bridge.failed)
            self.assertEqual(bridge.last_status, "needs_user")

    async def test_gui_budget_consumes_planning_and_spawn_time_without_resetting_deadline(self):
        process = FakeProcess()
        clock = [100.0]
        async def spawn(*_argv, **_kwargs):
            clock[0] += 5.0
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), \
                patch("gui_bridge.shutil.which", return_value=sys.executable), \
                patch("gui_bridge.monotonic", side_effect=lambda: clock[0]):
            bridge = self.make_bridge(deadline_monotonic=220.0)
            # The planner used70s after the bridge's original task began.
            clock[0] = 170.0
            await bridge.run("authorized desktop goal")
        self.assertEqual(process.stdin.lines[0]["options"]["timeoutMs"], 35000)
        self.assertEqual(bridge.deadline_monotonic, 220.0)

    async def test_insufficient_remaining_time_is_cached_without_starting_a_process(self):
        for remaining in (10.999, 10.0, 0.0, -5.0):
            with self.subTest(remaining=remaining), \
                    patch("gui_bridge.asyncio.create_subprocess_exec") as spawn, \
                    patch("gui_bridge.shutil.which", return_value=sys.executable), \
                    patch("gui_bridge.monotonic", return_value=200.0):
                bridge = self.make_bridge(deadline_monotonic=200.0 + remaining)
                first = await bridge.run("authorized desktop goal")
                again = await bridge.run("must not replay when planner asks again")
                self.assertEqual(first, again)
                self.assertEqual(json.loads(first)["status"], "timed_out")
                self.assertEqual(json.loads(first)["steps"], 0)
                self.assertEqual(bridge.last_status, "timed_out")
                self.assertFalse(bridge.failed)
                spawn.assert_not_called()

    async def test_deadline_expiring_during_spawn_never_sends_a_run_frame(self):
        process = FakeProcess()
        clock = [100.0]
        calls = []
        async def spawn(*args, **_kwargs):
            calls.append(args)
            clock[0] = 111.0
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), \
                patch("gui_bridge.shutil.which", return_value=sys.executable), \
                patch("gui_bridge.monotonic", side_effect=lambda: clock[0]):
            bridge = self.make_bridge(deadline_monotonic=120.0)
            first = await bridge.run("authorized desktop goal")
            again = await bridge.run("must not replay after spawn consumed the budget")
        self.assertEqual(json.loads(first)["status"], "timed_out")
        self.assertEqual(first, again)
        self.assertEqual(len(calls), 1)
        self.assertEqual([line["type"] for line in process.stdin.lines], ["abort"])
        self.assertIsNone(bridge.process)

    async def test_remaining_budget_keeps_configured_cap_and_unlimited_steps(self):
        for configured, expected in ((120, 120000), (900, 900000), (1800, 1800000)):
            with self.subTest(configured=configured):
                process = FakeProcess()
                async def spawn(*_argv, **_kwargs):
                    return process
                with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), \
                        patch("gui_bridge.shutil.which", return_value=sys.executable), \
                        patch("gui_bridge.monotonic", return_value=100.0):
                    bridge = self.make_bridge(deadline_monotonic=5000.0)
                    bridge.options.update(timeout_seconds=configured, max_steps=0)
                    await bridge.run("authorized desktop goal")
                options = process.stdin.lines[0]["options"]
                self.assertEqual(options["timeoutMs"], expected)
                self.assertEqual(options["maxSteps"], 0)

    async def test_unlimited_duration_survives_old_two_and_ten_minute_boundaries(self):
        process = FakeProcess(completed=False)
        clock = [100.0]
        async def spawn(*_argv, **_kwargs):
            # Planning/process startup can itself outlast both former limits.
            clock[0] += 3600
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), \
                patch("gui_bridge.shutil.which", return_value=sys.executable), \
                patch("gui_bridge.monotonic", side_effect=lambda: clock[0]):
            bridge = self.make_bridge()
            bridge.options.update(timeout_seconds=0, max_steps=0)
            task = asyncio.create_task(bridge.run("long authorized desktop goal"))
            await asyncio.sleep(0)
            self.assertEqual(process.stdin.lines[0]["options"]["timeoutMs"], 0)
            clock[0] += 24 * 3600
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            process.stdout.feed_data((json.dumps({"type": "final", "id": "gui-1", "status": "completed",
                "summary": "long goal completed", "steps": 12345,
                "verification": report("verified")}) + "\n").encode())
            result = json.loads(await task)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["steps"], 12345)

    async def test_unlimited_duration_remains_cancellable_after_large_clock_advance(self):
        process = FakeProcess(completed=False)
        clock = [0.0]
        async def spawn(*_argv, **_kwargs):
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), \
                patch("gui_bridge.shutil.which", return_value=sys.executable), \
                patch("gui_bridge.monotonic", side_effect=lambda: clock[0]):
            bridge = self.make_bridge()
            bridge.options["timeout_seconds"] = 0
            task = asyncio.create_task(bridge.run("long authorized desktop goal"))
            await asyncio.sleep(0)
            clock[0] = 86400
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(process.stdin.lines[-1]["type"], "abort")
            self.assertIsNone(bridge.process)
            self.assertEqual(json.loads(await bridge.run("must not replay"))["status"], "cancelled")

    async def test_cancel_sends_abort_and_releases_process(self):
        process = FakeProcess(completed=False)
        async def spawn(*_argv, **_kwargs):
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), patch("gui_bridge.shutil.which", return_value=sys.executable):
            bridge = self.make_bridge()
            task = asyncio.create_task(bridge.run("test desktop goal"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(process.stdin.lines[-1]["type"], "abort")
            self.assertIsNone(bridge.process)
            repeated = json.loads(await bridge.run("must not restart after cancellation"))
            self.assertEqual(repeated["status"], "cancelled")
            self.assertEqual(bridge.last_status, "cancelled")

    async def test_all_terminal_results_are_sticky_and_parallel_calls_share_one_dispatch(self):
        for status in ("completed", "needs_user", "cancelled", "timed_out"):
            with self.subTest(status=status):
                process = FakeProcess(completed=False)
                process.stdout.feed_data((json.dumps({"type": "final", "id": "gui-1", "status": status,
                    "summary": "original verified outcome test-key", "steps": 2,
                    "verification": report("verified") if status == "completed" else None}) + "\n").encode())
                spawns = []

                async def spawn(*args, **kwargs):
                    spawns.append(args)
                    await asyncio.sleep(0.01)
                    return process

                with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), patch("gui_bridge.shutil.which", return_value=sys.executable):
                    bridge = self.make_bridge()
                    execute = authorized_gui_callback(bridge, "original full authorized goal")
                    first, second = await asyncio.gather(execute(task="subtask one"), execute(task="subtask two"))
                    self.assertEqual(first, second)
                    self.assertEqual(await execute(task="third rewritten task"), first)
                    self.assertEqual(len(spawns), 1)
                    self.assertEqual(process.stdin.lines[0]["task"], "original full authorized goal")
                    self.assertEqual(json.loads(first)["status"], status)
                    self.assertNotIn("test-key", first)
                    self.assertEqual(bridge.steps, 2)
                    self.assertFalse(bridge.failed)

    async def test_unlimited_request_and_large_actual_step_count_are_preserved(self):
        for steps in (101, 1001, 12345):
            with self.subTest(steps=steps):
                process = FakeProcess(completed=False)
                process.stdout.feed_data((json.dumps({"type": "final", "id": "gui-1", "status": "completed",
                                                    "summary": "done", "steps": steps}) + "\n").encode())
                async def spawn(*_argv, **_kwargs):
                    return process
                with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), patch("gui_bridge.shutil.which", return_value=sys.executable):
                    bridge = self.make_bridge()
                    bridge.options["max_steps"] = 0
                    result = json.loads(await bridge.run("authorized task"))
                    self.assertEqual(process.stdin.lines[0]["options"]["maxSteps"], 0)
                    self.assertEqual(result["steps"], steps)

    async def failure_frame(self, frame):
        process = FakeProcess(completed=False)
        process.stdout.feed_data((json.dumps(frame) + "\n").encode())
        process.stdout.feed_eof()
        async def spawn(*_argv, **_kwargs):
            return process
        with patch("gui_bridge.asyncio.create_subprocess_exec", spawn), patch("gui_bridge.shutil.which", return_value=sys.executable):
            bridge = self.make_bridge()
            with self.assertRaises(BridgeFailure) as caught:
                await bridge.run("authorized desktop goal")
            self.assertIsNone(bridge.process)
            self.assertTrue(bridge.failed)
            self.assertEqual(bridge.failure_code, caught.exception.code)
            return caught.exception.code

    async def test_fixed_gui_error_codes_survive_without_child_messages(self):
        for code in ("SDK_UNAVAILABLE", "SCREENSHOT_FAILED", "MODEL_FAILED", "ACTION_FAILED",
                     "DESKTOP_LOCKED", "DESKTOP_SESSION_UNAVAILABLE", "SCREENSHOT_DISPLAY_UNAVAILABLE", "SCREENSHOT_BLACK_FRAME",
                     "UNSUPPORTED_ACTION", "SDK_FAILED", "STEP_LIMIT", "WORKER_FAILED"):
            with self.subTest(code=code):
                mapped = await self.failure_frame({"type": "error", "id": "gui-1", "code": code,
                                                   "message": "test-key private user task and model thought"})
                self.assertEqual(mapped, "GUI_" + code)

    async def test_unknown_error_code_and_message_cannot_escape(self):
        self.assertEqual(await self.failure_frame({"type": "error", "id": "gui-1",
                                                  "code": "test-key", "message": "test-key"}), "GUI_FAILED")

    async def test_malformed_gui_frame_is_protocol_failure(self):
        for frame in ([], {"type": "final", "id": "gui-1", "status": "completed", "steps": "secret"},
                      {"type": "final", "id": "gui-1", "status": "completed", "steps": True},
                      {"type": "final", "id": "gui-1", "status": "completed", "steps": -1},
                      {"type": "final", "id": "gui-1", "status": "completed", "steps": 1, "summary": {"key": "test-key"}},
                      {"type": "unknown", "id": "gui-1"}):
            with self.subTest(frame=frame):
                self.assertEqual(await self.failure_frame(frame), "GUI_PROTOCOL")

    async def test_missing_node_is_static_failure_before_child_spawn(self):
        with patch("gui_bridge.shutil.which", return_value=None), patch("gui_bridge.asyncio.create_subprocess_exec") as spawn:
            bridge = self.make_bridge()
            with self.assertRaises(BridgeFailure) as caught:
                await bridge.run("authorized task")
            self.assertEqual(caught.exception.code, "GUI_RUNTIME_MISSING")
            self.assertEqual(bridge.failure_code, "GUI_RUNTIME_MISSING")
            spawn.assert_not_called()

    async def test_spawn_failure_is_static_and_first_failure_survives_retry(self):
        spawns = []
        async def spawn(*_args, **_kwargs):
            spawns.append(True)
            raise OSError("test-key and secret path")
        with patch("gui_bridge.shutil.which", return_value=sys.executable), patch("gui_bridge.asyncio.create_subprocess_exec", spawn):
            bridge = self.make_bridge()
            with self.assertRaises(BridgeFailure) as caught:
                await bridge.run("authorized task")
            self.assertEqual(str(caught.exception), "GUI_START_FAILED")
            with self.assertRaises(BridgeFailure) as repeated:
                await bridge.run("retry must not spawn")
            self.assertEqual(repeated.exception.code, "GUI_START_FAILED")
            self.assertEqual(len(spawns), 1)
            bridge.record_failure("GUI_ALREADY_USED")
            self.assertEqual(bridge.failure_code, "GUI_START_FAILED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
