/** Synthetic images and fake Win32 process boundaries only; no desktop pixels. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { resolve } from 'node:path';
import { Jimp } from 'jimp';
import { GUIAgent } from '@ui-tars/sdk';
import { parseBoxToScreenCoords } from '@ui-tars/sdk/core';
import { createSdkAgent } from '../sdk-adapter.mjs';
import { BridgeError } from '../bridge.mjs';
import { assertCaptureDesktop, captureWindowsScreen, windowsCaptureHelper } from '../windows-capture.mjs';

const signal = () => new AbortController().signal;
const PYTHON = resolve('host-selected-runtime', 'python.exe');
const META = { bounds: [0, 0, 64, 48], monitors: [[0, 0, 64, 48]], primary: true, backend: 'win32-gdi' };
const png = async (color, width = 64, height = 48) =>
  (await new Jimp({ width, height, color }).getBuffer('image/png')).toString('base64');
const WHITE = await png(0xffffffff), BLACK = await png(0x000000ff);
const frame = (base64 = WHITE, meta = META) => ({ ...meta, base64, scaleFactor: 1 });

test('fresh native capture is preferred and does not invoke SDK capture', async () => {
  const calls = []; let sdk = 0;
  const output = await captureWindowsScreen(signal(), PYTHON, () => { sdk++; }, async (mode) => {
    calls.push(mode); return frame();
  });
  assert.equal(sdk, 0); assert.deepEqual(calls, ['capture']);
  assert.equal(output.base64, WHITE); assert.equal(output.capture.backend, 'win32-gdi');
  assert.ok(!Object.hasOwn(output.capture, 'base64'));
});

test('native cold failure falls back to a fresh, checked matching SDK frame', async () => {
  const calls = []; let sdk = 0;
  const output = await captureWindowsScreen(signal(), PYTHON, async () => { sdk++; return frame(); }, async (mode) => {
    calls.push(mode); if (mode === 'capture') throw new Error('private native failure'); return META;
  });
  assert.equal(sdk, 1); assert.deepEqual(calls, ['capture', 'probe', 'probe']);
  assert.equal(output.capture.backend, 'nutjs');
});

test('fractional Windows DPI keeps native/fallback images and NutJS input in the same logical domain', async () => {
  // The real 150% display reports physical 2560x1600 and logical 1707x1067:
  // X and Y density differ slightly due to integer rounding.
  const meta = { bounds: [0, 0, 2560, 1600], monitors: [[0, 0, 2560, 1600]], primary: true,
    inputBounds: [0, 0, 1707, 1067], inputMonitors: [[0, 0, 1707, 1067]], dpiAwareness: 0, backend: 'win32-gdi' };
  const physical = await png(0xffffffff, 2560, 1600);
  const logical = await png(0xffffffff, 1707, 1067);
  const native = await captureWindowsScreen(signal(), PYTHON, () => { throw new Error('unexpected fallback'); },
    async () => frame(physical, meta));
  const decoded = await Jimp.fromBuffer(Buffer.from(native.base64, 'base64'));
  assert.equal(decoded.width, 1707); assert.equal(decoded.height, 1067);
  assert.equal(native.scaleFactor, 2560 / 1707);
  assert.deepEqual(native.capture.inputBounds, [0, 0, 1707, 1067]);
  for (const fraction of [.25, .5, .75]) {
    const { x, y } = parseBoxToScreenCoords({ boxStr: `[${fraction},${fraction}]`, screenWidth: decoded.width, screenHeight: decoded.height });
    // libnut 2.7.1's primary-display SendInput normalization must land at the
    // same proportion in both physical axes, with only rounding tolerance.
    const physicalX = Math.ceil(x * 65536 / 1707) * 2560 / 65536;
    const physicalY = Math.ceil(y * 65536 / 1067) * 1600 / 65536;
    assert.ok(Math.abs(physicalX - fraction * 2560) < 2);
    assert.ok(Math.abs(physicalY - fraction * 1600) < 2);
  }
  let sdk = 0;
  const fallback = await captureWindowsScreen(signal(), PYTHON, async () => {
    sdk++; return { base64: logical, scaleFactor: 2560 / 1707 };
  }, async (mode) => { if (mode === 'capture') throw new Error('transient'); return meta; });
  assert.equal(sdk, 1); assert.equal(fallback.capture.backend, 'nutjs');
  assert.equal(fallback.base64, logical); assert.equal(fallback.scaleFactor, native.scaleFactor);
});

test('only observations are retried, with a fixed two-attempt bound', async () => {
  let captures = 0, sdk = 0;
  const output = await captureWindowsScreen(signal(), PYTHON, async () => { sdk++; throw new Error('native backend down'); }, async (mode) => {
    if (mode === 'probe') return META;
    if (++captures === 1) throw new Error('cold transient');
    return frame();
  });
  assert.equal(captures, 2); assert.equal(sdk, 1); assert.equal(output.capture.backend, 'win32-gdi');
  captures = sdk = 0;
  await assert.rejects(captureWindowsScreen(signal(), PYTHON, async () => { sdk++; throw new Error('secret'); }, async (mode) => {
    if (mode === 'probe') return META;
    captures++; throw new Error('private failure');
  }), { code: 'SCREENSHOT_FAILED' });
  assert.equal(captures, 2); assert.equal(sdk, 2);
});

test('locked/disconnected desktops stop immediately without fallback or recovery input', async () => {
  for (const code of ['DESKTOP_LOCKED', 'DESKTOP_SESSION_UNAVAILABLE']) {
    let helpers = 0, sdk = 0;
    await assert.rejects(captureWindowsScreen(signal(), PYTHON, () => { sdk++; }, async () => {
      helpers++; throw new BridgeError(code);
    }), { code });
    assert.equal(helpers, 1); assert.equal(sdk, 0);
  }
});

test('black or wrong-sized frames are rejected, never replaced by cached synthetic pixels', async () => {
  await assert.rejects(captureWindowsScreen(signal(), PYTHON, async () => frame(BLACK), async (mode) =>
    mode === 'probe' ? META : frame(BLACK)), { code: 'SCREENSHOT_BLACK_FRAME' });
  const wrong = await png(0xffffffff, 32, 24);
  await assert.rejects(captureWindowsScreen(signal(), PYTHON, async () => frame(wrong), async (mode) =>
    mode === 'probe' ? META : frame(wrong)), { code: 'SCREENSHOT_DISPLAY_UNAVAILABLE' });
});

test('secondary monitor negative origins are detected and never paired with primary-only pointer input', async () => {
  const secondary = { bounds: [-64, -48, 0, 0], monitors: [[-64, -48, 0, 0], [0, 0, 64, 48]], primary: false };
  let sdk = 0;
  await assert.rejects(captureWindowsScreen(signal(), PYTHON, () => { sdk++; }, async () => frame(WHITE, secondary)),
    { code: 'SCREENSHOT_DISPLAY_UNAVAILABLE' });
  assert.equal(sdk, 0);
  let attempts = 0;
  await assert.rejects(captureWindowsScreen(signal(), PYTHON, () => { sdk++; }, async (mode) => {
    if (mode === 'probe') return secondary;
    attempts++; throw new Error('failure');
  }), { code: 'SCREENSHOT_FAILED' });
  assert.equal(sdk, 0); assert.equal(attempts, 2);
});

test('desktop or topology changes between observation and input block the action', async () => {
  await assert.rejects(assertCaptureDesktop(META, signal(), PYTHON, async () => { throw new BridgeError('DESKTOP_LOCKED'); }), { code: 'DESKTOP_LOCKED' });
  await assert.rejects(assertCaptureDesktop(META, signal(), PYTHON, async () => ({ ...META, bounds: [64, 0, 128, 48] })), { code: 'SCREENSHOT_DISPLAY_UNAVAILABLE' });
  await assertCaptureDesktop(META, signal(), PYTHON, async () => META);
  await assert.rejects(assertCaptureDesktop(META, signal(), PYTHON, async () =>
    ({ ...META, inputBounds: [0, 0, 32, 24], inputMonitors: [[0, 0, 32, 24]] })), { code: 'SCREENSHOT_DISPLAY_UNAVAILABLE' });
});

function helperProcess(result, exitCode = 0, automatic = true) {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stdin = new EventEmitter();
  child.stdin.end = (value) => { child.input = value; if (automatic) queueMicrotask(() => {
    child.stdout.emit('data', Buffer.from(JSON.stringify(result))); child.emit('close', exitCode);
  }); };
  child.kill = () => { child.killed = true; };
  const calls = [];
  return { child, calls, spawn(...args) { calls.push(args); return child; } };
}

test('helper has fixed no-shell arguments, private stdio and fixed safe error codes', async () => {
  const fake = helperProcess({ ok: true, ...META });
  await windowsCaptureHelper('probe', signal(), PYTHON, fake.spawn);
  assert.deepEqual(JSON.parse(fake.child.input), { mode: 'probe', inputPid: process.pid });
  assert.equal(fake.calls.length, 1); assert.equal(fake.calls[0][0], PYTHON);
  assert.deepEqual(fake.calls[0][1].slice(0, 2), ['-I', '-u']);
  assert.ok(fake.calls[0][1][2].endsWith('windows-screen-capture.py'));
  assert.equal(fake.calls[0][2].windowsHide, true); assert.equal(fake.calls[0][2].shell, undefined);
  const bad = helperProcess({ ok: false, code: 'SECRET_INTERNAL_NATIVE_TEXT' }, 1);
  await assert.rejects(windowsCaptureHelper('capture', signal(), PYTHON, bad.spawn), { code: 'SCREENSHOT_FAILED' });
  const locked = helperProcess({ ok: false, code: 'DESKTOP_LOCKED' }, 1);
  await assert.rejects(windowsCaptureHelper('capture', signal(), PYTHON, locked.spawn), { code: 'DESKTOP_LOCKED' });
});

test('cancellation kills only the owned helper and performs no SDK fallback', async () => {
  const fake = helperProcess({}, 0, false); const controller = new AbortController();
  const pending = windowsCaptureHelper('capture', controller.signal, PYTHON, fake.spawn);
  controller.abort(); assert.equal(fake.child.killed, true);
  fake.child.emit('close', null);
  await assert.rejects(pending, { name: 'AbortError' });
});

test('real SDK receives a precise capture error before any model call or native action', async () => {
  let screenshots = 0, executes = 0, modelCalls = 0;
  class FakeOperator {
    static MANUAL = { ACTION_SPACES: ['finished()'] };
    async screenshot() { screenshots++; return frame(BLACK); }
    async execute() { executes++; }
  }
  const original = globalThis.fetch;
  globalThis.fetch = async () => { modelCalls++; throw new Error('model must not see unusable pixels'); };
  try {
    const events = [];
    const handle = await createSdkAgent({ config: { baseURL: 'http://127.0.0.1:1/v1', apiKey: 'fake-key',
      model: 'fake', provider: 'ui-tars-1.5' }, options: { maxSteps: 1, pythonExecutable: PYTHON }, signal: signal(), onEvent: (e) => events.push(e) },
    { GUIAgent, NutJSOperator: FakeOperator, captureScreen: (s, p, fallback) =>
      captureWindowsScreen(s, p, fallback, async (mode) => mode === 'probe' ? META : frame(BLACK)) });
    await assert.rejects(handle.run('synthetic inaccessible capture'), { code: 'SCREENSHOT_BLACK_FRAME' });
    assert.equal(modelCalls, 0); assert.equal(executes, 0); assert.equal(screenshots, 2);
    assert.deepEqual(events.map((e) => e.status), ['running', 'failed']);
  } finally { globalThis.fetch = original; }
});

test('real SDK stops native dispatch if the desktop locks after the model saw a valid frame', async () => {
  let executes = 0, modelCalls = 0;
  class FakeOperator {
    static MANUAL = { ACTION_SPACES: ["click(start_box='[x1,y1,x2,y2]')"] };
    async screenshot() { return { ...frame(), capture: META }; }
    async execute() { executes++; }
  }
  const original = globalThis.fetch;
  globalThis.fetch = async () => {
    modelCalls++;
    return new Response(JSON.stringify({ id: 'synthetic-lock', object: 'chat.completion', created: 1, model: 'fake',
      choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant',
        content: "Thought: synthetic\nAction: click(start_box='(100,200)')" } }] }),
    { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    const events = [];
    const handle = await createSdkAgent({ config: { baseURL: 'http://127.0.0.1:1/v1', apiKey: 'fake-key',
      model: 'fake', provider: 'ui-tars-1.5' }, options: { maxSteps: 1, pythonExecutable: PYTHON }, signal: signal(), onEvent: (e) => events.push(e) },
    { GUIAgent, NutJSOperator: FakeOperator, checkDesktop: async () => { throw new BridgeError('DESKTOP_LOCKED'); } });
    await assert.rejects(handle.run('synthetic lock between observation and input'), { code: 'DESKTOP_LOCKED' });
    assert.equal(modelCalls, 1); assert.equal(executes, 0);
    assert.deepEqual(events.filter((e) => e.stage === 'acting').map((e) => e.status), ['running', 'failed']);
  } finally { globalThis.fetch = original; }
});
