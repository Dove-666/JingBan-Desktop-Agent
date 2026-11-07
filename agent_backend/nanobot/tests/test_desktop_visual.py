"""Read-only visual observations with synthetic pixels and an in-memory provider.

No test captures the user's desktop, starts a real helper or calls a model.
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_visual as visual


def png(width=2, height=2):
    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff))
    rows = (b'\0' + bytes([40, 100, 180]) * width) * height
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def frame():
    return {'ok': True, 'base64': base64.b64encode(png()).decode('ascii'),
            'width': 2, 'height': 2, 'bounds': [0, 0, 2, 2], 'primary': True,
            'monitors': [[0, 0, 2, 2]], 'inputBounds': [0, 0, 2, 2],
            'inputMonitors': [[0, 0, 2, 2]], 'dpiAwareness': 2,
            'backend': 'win32-gdi', 'scaleFactor': 1}


class Provider:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or SimpleNamespace(
            content='A white heart beside a video. The title is visible.',
            finish_reason='stop', tool_calls=[])

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class Input:
    def __init__(self): self.payload = b''; self.closed = False
    def write(self, value): self.payload += value
    async def drain(self): pass
    def close(self): self.closed = True


class Output:
    def __init__(self, raw=None):
        self.raw = raw
        self.started = asyncio.Event()
        self.cancelled = False

    async def read(self, count):
        self.started.set()
        if self.raw is None:
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        value, self.raw = self.raw[:count], self.raw[count:]
        return value


class Process:
    def __init__(self, raw=None, *, cleanup_fails=False):
        self.stdin, self.stdout = Input(), Output(raw)
        self.returncode = 0 if raw is not None else None
        self.kills = 0
        self.waits = 0
        self.cleanup_fails = cleanup_fails

    def kill(self):
        self.kills += 1
        if not self.cleanup_fails: self.returncode = -9

    async def wait(self):
        self.waits += 1
        if self.cleanup_fails: await asyncio.Event().wait()
        return self.returncode


class DesktopVisualTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, response=None):
        captured = []
        async def capture():
            captured.append(True)
            return frame()
        provider = Provider(response)
        return visual.DesktopVisualObserver(provider, 'fixture-model', capture=capture), provider, captured

    async def test_returns_fact_text_to_parent_without_goal_verification_or_pixels(self):
        observer, provider, captured = await self.make()
        result = json.loads(await observer.observe('What is the current heart state?'))
        self.assertEqual(len(captured), 1)
        self.assertEqual(result['status'], 'observed')
        self.assertIn('white heart', result['observation'])
        self.assertEqual(result['scope'], 'visual_observation_only')
        self.assertFalse(result['task_verified'])
        self.assertEqual(result['display']['bounds'], [0, 0, 2, 2])
        self.assertIn('observed_at', result)
        self.assertNotIn('base64', json.dumps(result))
        self.assertNotIn(frame()['base64'], json.dumps(result))
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call['model'], 'fixture-model')
        self.assertLessEqual(call['max_tokens'], 2000)
        self.assertNotIn('tools', call)
        self.assertEqual(call['messages'][0]['role'], 'system')
        content = call['messages'][-1]['content']
        self.assertEqual(content[1]['image_url']['url'], 'data:image/png;base64,' + frame()['base64'])

    async def test_default_question_is_bounded_reference_data(self):
        observer, provider, _ = await self.make()
        await observer.observe()
        self.assertIn('not instructions', provider.calls[0]['messages'][0]['content'])
        self.assertIn('off-screen', provider.calls[0]['messages'][0]['content'])
        self.assertIn('Question:', provider.calls[0]['messages'][-1]['content'][0]['text'])

    async def test_invalid_question_never_captures_or_calls_model(self):
        observer, provider, captured = await self.make()
        for question in [None, 3, 'x' * 2001, 'unsafe\0question']:
            with self.subTest(question_type=type(question).__name__):
                with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_INVALID_QUESTION'):
                    await observer.observe(question)
        self.assertEqual(captured, [])
        self.assertEqual(provider.calls, [])

    async def test_capture_error_has_fixed_code_and_no_provider_call(self):
        async def capture(): raise RuntimeError('secret raw native exception')
        provider = Provider()
        observer = visual.DesktopVisualObserver(provider, 'fixture', capture=capture)
        with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_CAPTURE_FAILED$') as raised:
            await observer.observe()
        self.assertEqual(raised.exception.effect_state, 'not_dispatched')
        self.assertEqual(provider.calls, [])

    async def test_invalid_pixels_and_metadata_never_reach_model(self):
        invalid = []
        for key, value in [('base64', 'not base64'), ('width', True), ('width', 10000),
                           ('height', 10000), ('bounds', [0, 0, 20, 2]), ('ok', False)]:
            current = frame(); current[key] = value; invalid.append(current)
        for raw in [b'invalid', png()[:-1], png() + b'trailing', png()[:-10] + b'\0' * 10]:
            current = frame(); current['base64'] = base64.b64encode(raw).decode('ascii'); invalid.append(current)
        for capture_result in invalid:
            async def capture(): return capture_result
            provider = Provider()
            with self.subTest(keys=list(capture_result)):
                with self.assertRaises(visual.DesktopVisualError):
                    await visual.DesktopVisualObserver(provider, 'fixture', capture=capture).observe()
            self.assertEqual(provider.calls, [])

    async def test_response_rejects_tool_calls_errors_empty_and_oversize_content(self):
        responses = [SimpleNamespace(content='text', tool_calls=[{'function': 'click'}], finish_reason='stop'),
                     SimpleNamespace(content='native failure details', tool_calls=[], finish_reason='error'),
                     SimpleNamespace(content='', tool_calls=[], finish_reason='stop'),
                     SimpleNamespace(content={'hidden': 'state'}, tool_calls=[], finish_reason='stop'),
                     SimpleNamespace(content='x' * 16001, tool_calls=[], finish_reason='stop')]
        for response in responses:
            observer, _, _ = await self.make(response)
            with self.subTest(response_type=type(response.content).__name__):
                with self.assertRaises(visual.DesktopVisualError): await observer.observe()

    async def test_provider_errors_do_not_echo_credentials_or_raw_exceptions(self):
        class Broken:
            async def chat(self, **kwargs): raise RuntimeError('sk-fixture-private raw exception')
        async def capture(): return frame()
        observer = visual.DesktopVisualObserver(Broken(), 'fixture', capture=capture)
        with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_MODEL_UNAVAILABLE$'):
            await observer.observe()

    async def test_secret_redaction_and_reasoning_exclusion(self):
        secret = 'fixture-secret-\\value'
        response = SimpleNamespace(content='Visible: ' + secret + ' / ' + json.dumps(secret)[1:-1],
                                   reasoning_content='NEVER RETURN THIS PRIVATE FIELD',
                                   tool_calls=[], finish_reason='stop')
        async def capture(): return frame()
        provider = Provider(response)
        observer = visual.DesktopVisualObserver(provider, 'fixture', secrets=(secret,), capture=capture)
        result = await observer.observe('Read ' + secret)
        self.assertNotIn(secret, result)
        self.assertNotIn(json.dumps(secret)[1:-1], json.loads(result)['observation'])
        self.assertNotIn('NEVER RETURN', result)
        self.assertNotIn(secret, provider.calls[0]['messages'][-1]['content'][0]['text'])
        self.assertIn('[redacted]', result)

    async def test_truncated_text_keeps_observation_scope_and_explicit_label(self):
        response = SimpleNamespace(content='fact ' * 2000, tool_calls=[], finish_reason='stop')
        observer, _, _ = await self.make(response)
        result = json.loads(await observer.observe())
        self.assertEqual(len(result['observation']), 8000)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['task_verified'])

    async def test_embedded_reasoning_tags_are_rejected_instead_of_archived(self):
        response = SimpleNamespace(content='<think>private chain</think>Visible facts', tool_calls=[], finish_reason='stop')
        observer, _, _ = await self.make(response)
        with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_RESPONSE_INVALID$'):
            await observer.observe()

    async def test_model_timeout_is_per_request_and_has_no_retry(self):
        class Slow(Provider):
            async def chat(self, **kwargs):
                self.calls.append(kwargs)
                await asyncio.Event().wait()
        provider = Slow()
        async def capture(): return frame()
        observer = visual.DesktopVisualObserver(provider, 'fixture', capture=capture)
        with patch.object(visual, 'MODEL_TIMEOUT_SECONDS', .01):
            with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_MODEL_UNAVAILABLE'):
                await observer.observe()
        self.assertEqual(len(provider.calls), 1)

    async def test_cancellation_during_model_request_propagates(self):
        started = asyncio.Event()
        class Slow:
            async def chat(self, **kwargs): started.set(); await asyncio.Event().wait()
        async def capture(): return frame()
        task = asyncio.create_task(visual.DesktopVisualObserver(Slow(), 'fixture', capture=capture).observe())
        await started.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_cleanup_failure_latches_observer_until_task_ends(self):
        attempts = []
        async def capture():
            attempts.append(1)
            raise visual.DesktopVisualError('DESKTOP_VISUAL_CLEANUP_UNCONFIRMED')
        observer = visual.DesktopVisualObserver(Provider(), 'fixture', capture=capture)
        for _ in range(2):
            with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_CLEANUP_UNCONFIRMED'):
                await observer.observe()
        self.assertEqual(len(attempts), 1)

    async def test_parent_readiness_before_and_after_normal_observation(self):
        observer, _, _ = await self.make()
        self.assertIsNone(observer.assert_parent_ready())
        await observer.observe()
        self.assertIsNone(observer.assert_parent_ready())

    async def test_unconfirmed_cleanup_blocks_parent_readiness(self):
        async def capture():
            raise visual.DesktopVisualError('DESKTOP_VISUAL_CLEANUP_UNCONFIRMED')
        observer = visual.DesktopVisualObserver(Provider(), 'fixture', capture=capture)
        with self.assertRaises(visual.DesktopVisualError): await observer.observe()
        with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_CLEANUP_UNCONFIRMED$') as raised:
            observer.assert_parent_ready()
        self.assertEqual(raised.exception.effect_state, 'not_dispatched')

    async def test_model_failure_does_not_block_parent_readiness(self):
        class Broken:
            async def chat(self, **kwargs): raise RuntimeError('model unavailable')
        async def capture(): return frame()
        observer = visual.DesktopVisualObserver(Broken(), 'fixture', capture=capture)
        with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_MODEL_UNAVAILABLE$'):
            await observer.observe()
        self.assertIsNone(observer.assert_parent_ready())


class CaptureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_is_fixed_hidden_isolated_and_has_no_credentials_in_argv_or_env(self):
        process = Process(json.dumps(frame()).encode())
        launch = AsyncMock(return_value=process)
        with patch.object(visual.sys, 'platform', 'win32'), patch.object(visual.asyncio, 'create_subprocess_exec', launch), \
                patch.dict(os.environ, {'AURA_API_KEY': 'do-not-forward', 'CUSTOM_TOKEN': 'do-not-forward'}):
            value = await visual.capture_desktop()
        args, kwargs = launch.call_args
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[1], '-I')
        self.assertEqual(Path(args[2]).name, 'windows-screen-capture.py')
        self.assertTrue(Path(args[2]).is_absolute())
        self.assertEqual(kwargs['creationflags'], 0x08000000)
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('AURA_API_KEY', kwargs['env'])
        self.assertNotIn('CUSTOM_TOKEN', kwargs['env'])
        self.assertEqual(json.loads(process.stdin.payload), {'mode': 'capture', 'inputPid': os.getpid()})
        self.assertTrue(process.stdin.closed)
        self.assertEqual(process.kills, 0)
        self.assertGreaterEqual(process.waits, 1)
        self.assertEqual(value['base64'], frame()['base64'])

    async def test_cancellation_stops_owned_helper_and_waits(self):
        process = Process()
        with patch.object(visual.sys, 'platform', 'win32'), \
                patch.object(visual.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(visual.capture_desktop())
            await process.stdout.started.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(process.kills, 1)
        self.assertGreaterEqual(process.waits, 1)
        self.assertTrue(process.stdout.cancelled)

    async def test_timeout_stops_owned_helper_and_waits(self):
        process = Process()
        with patch.object(visual.sys, 'platform', 'win32'), \
                patch.object(visual.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), \
                patch.object(visual, 'CAPTURE_TIMEOUT_SECONDS', .01):
            with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_CAPTURE_TIMEOUT'):
                await visual.capture_desktop()
        self.assertEqual(process.kills, 1)
        self.assertGreaterEqual(process.waits, 1)

    async def test_oversized_stdout_is_bounded_and_never_returned(self):
        process = Process(b'x' * 100)
        with patch.object(visual.sys, 'platform', 'win32'), \
                patch.object(visual.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), \
                patch.object(visual, 'MAX_WIRE_BYTES', 50):
            with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_CAPTURE_INVALID'):
                await visual.capture_desktop()
        self.assertGreaterEqual(process.waits, 1)

    async def test_native_failure_propagates_only_known_static_reason(self):
        process = Process(b'{"ok":false,"code":"DESKTOP_LOCKED","message":"secret-details"}')
        with patch.object(visual.sys, 'platform', 'win32'), \
                patch.object(visual.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_DESKTOP_LOCKED$'):
                await visual.capture_desktop()

    async def test_unconfirmed_helper_cleanup_is_explicit(self):
        process = Process(cleanup_fails=True)
        with patch.object(visual.sys, 'platform', 'win32'), \
                patch.object(visual.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), \
                patch.object(visual, 'CAPTURE_TIMEOUT_SECONDS', .01), \
                patch.object(visual, 'CLEANUP_TIMEOUT_SECONDS', .01):
            with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_CLEANUP_UNCONFIRMED'):
                await visual.capture_desktop()
        self.assertEqual(process.kills, 1)

    async def test_failed_termination_is_unknown_cleanup_and_not_a_raw_exception(self):
        class KillDenied(Process):
            def kill(self):
                self.kills += 1
                raise PermissionError('sensitive native termination detail')
        process = KillDenied(cleanup_fails=True)
        with patch.object(visual.sys, 'platform', 'win32'), \
                patch.object(visual.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), \
                patch.object(visual, 'CAPTURE_TIMEOUT_SECONDS', .01), \
                patch.object(visual, 'CLEANUP_TIMEOUT_SECONDS', .01):
            with self.assertRaisesRegex(visual.DesktopVisualError, '^DESKTOP_VISUAL_CLEANUP_UNCONFIRMED$'):
                await visual.capture_desktop()
        self.assertEqual(process.kills, 1)
        self.assertGreaterEqual(process.waits, 1)

    async def test_unsupported_platform_never_spawns(self):
        launch = AsyncMock()
        with patch.object(visual.sys, 'platform', 'linux'), patch.object(visual.asyncio, 'create_subprocess_exec', launch):
            with self.assertRaisesRegex(visual.DesktopVisualError, 'DESKTOP_VISUAL_UNSUPPORTED'):
                await visual.capture_desktop()
        launch.assert_not_called()


if __name__ == '__main__': unittest.main()
