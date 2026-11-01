/** Virtual task clocks, real SDK with synthetic pixels; no desktop input. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { GUIAgent } from '@ui-tars/sdk';
import { Jimp } from 'jimp';
import { BridgeController, validateRun } from '../bridge.mjs';
import { createSdkAgent } from '../sdk-adapter.mjs';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const request = (options) => ({ type: 'run', id: 'long-task', task: 'Only an isolated synthetic goal',
  config: { baseURL: 'http://127.0.0.1:9/v1', model: 'offline-model', apiKey: 'qa-only-key' },
  ...(options !== undefined && { options }) });

class Clock {
  now = 0;
  next = 1;
  created = 0;
  cleared = 0;
  timers = new Map();
  setTimer = (callback, delay) => {
    this.created++;
    const id = this.next++;
    this.timers.set(id, { at: this.now + delay, callback });
    return id;
  };
  clearTimer = (id) => { this.cleared++; this.timers.delete(id); };
  advance(delay) {
    this.now += delay;
    for (const [id, timer] of this.timers) {
      if (timer.at <= this.now) { this.timers.delete(id); timer.callback(); }
    }
  }
}

test('omitted and zero task budgets stay zero on the wire; explicit finite budgets are not capped at ten minutes', () => {
  for (const options of [undefined, {}, { timeoutMs: 0 }, { timeout_ms: 0 }]) {
    assert.equal(validateRun(request(options)).options.timeoutMs, 0);
  }
  for (const timeoutMs of [1000, 120000, 600000, 900000, 2_147_483_647]) {
    assert.equal(validateRun(request({ timeoutMs })).options.timeoutMs, timeoutMs);
  }
  for (const timeoutMs of [-1, 999, 0.5, Infinity, NaN, '0', true, 2_147_483_648]) {
    assert.throws(() => validateRun(request({ timeoutMs })), { code: 'INVALID_REQUEST' });
  }
});

test('unlimited tasks survive virtual two-minute and ten-minute boundaries and can still be stopped', async () => {
  for (const options of [undefined, {}, { timeoutMs: 0 }]) {
    const clock = new Clock();
    const events = [];
    let signal, finish;
    let stops = 0;
    const bridge = new BridgeController({ emit: (event) => events.push(event),
      setTimer: clock.setTimer, clearTimer: clock.clearTimer,
      createAgent: async (run) => {
        signal = run.signal;
        return { run: () => new Promise((resolve) => { finish = resolve; }), stop() { stops++; } };
      } });
    bridge.handle(request(options));
    await tick();
    for (const elapsed of [120001, 600001, 86_400_000]) {
      clock.advance(elapsed);
      assert.equal(signal.aborted, false);
      assert.ok(bridge.active);
      assert.equal(events.filter((event) => event.type === 'final').length, 0);
    }
    assert.equal(clock.created, 0);
    bridge.handle({ type: 'abort', id: 'long-task' });
    assert.equal(signal.aborted, true);
    assert.equal(stops, 1);
    finish({ status: 'completed', summary: 'late completion after Stop' });
    await tick();
    assert.equal(events.at(-1).status, 'cancelled');
    assert.equal(events.filter((event) => event.type === 'final').length, 1);
    assert.equal(clock.cleared, 0);
    assert.equal(bridge.active, null);
  }
});

test('closing the parent cancels an unlimited task even while SDK creation is pending', async () => {
  const clock = new Clock();
  const events = [];
  let signal, ready;
  let stops = 0;
  const bridge = new BridgeController({ emit: (event) => events.push(event),
    setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    createAgent: (run) => {
      signal = run.signal;
      return new Promise((resolve) => { ready = resolve; });
    } });
  bridge.handle(request());
  bridge.close();
  assert.equal(signal.aborted, true);
  ready({ stop() { stops++; }, async run() {
    // The real SDK adapter checks this signal before capture or native input.
    assert.equal(signal.aborted, true);
    throw new DOMException('cancelled', 'AbortError');
  } });
  await tick();
  assert.equal(stops, 1);
  assert.equal(events.at(-1).status, 'cancelled');
  assert.equal(events.filter((event) => event.type === 'final').length, 1);
  assert.equal(clock.created, 0);
  bridge.handle(request());
  assert.equal(bridge.active, null);
});

test('explicit positive task budgets still expire once and remain terminal after late SDK completion', async () => {
  const clock = new Clock(); const events = [];
  let finish, signal;
  const bridge = new BridgeController({ emit: (event) => events.push(event),
    setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    createAgent: async (run) => { signal = run.signal; return {
      run: () => new Promise((resolve) => { finish = resolve; }), stop() {},
    }; } });
  bridge.handle(request({ timeoutMs: 900000 }));
  await tick();
  clock.advance(600001);
  assert.equal(signal.aborted, false);
  clock.advance(300000);
  assert.equal(signal.aborted, true);
  finish({ status: 'completed' });
  await tick();
  assert.equal(events.at(-1).status, 'timed_out');
  assert.equal(events.filter((event) => event.type === 'final').length, 1);
  assert.equal(clock.created, 1);
  assert.equal(clock.cleared, 1);
});

test('unlimited whole tasks retain the installed SDK single-model-request timeout', async (t) => {
  const frame = (await new Jimp({ width: 32, height: 32, color: 0xffffffff }).getBuffer('image/png')).toString('base64');
  let received;
  const fetching = new Promise((resolve) => { received = resolve; });
  let calls = 0; let aborted = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (_input, init) => {
    calls++;
    return new Promise((_resolve, reject) => {
      const cancel = () => { aborted = true; reject(init.signal.reason); };
      init.signal.addEventListener('abort', cancel, { once: true });
      received();
    });
  };
  class FakeOperator {
    static MANUAL = { ACTION_SPACES: ['finished()'] };
    static actions = 0;
    async screenshot() { return { base64: frame, scaleFactor: 1 }; }
    async execute() { FakeOperator.actions++; }
  }
  t.mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const validated = validateRun(request());
    const handle = await createSdkAgent({ ...validated, signal: new AbortController().signal, onEvent() {} },
      { GUIAgent, NutJSOperator: FakeOperator });
    const pending = handle.run(validated.task);
    // Install a rejection handler before advancing the SDK's real timeout.
    const failed = assert.rejects(pending, (exception) => ['MODEL_FAILED', 'SDK_FAILED'].includes(exception.code));
    await fetching;
    t.mock.timers.tick(29_999);
    assert.equal(aborted, false);
    t.mock.timers.tick(2);
    await failed;
    assert.equal(aborted, true);
    assert.equal(calls, 1);
    assert.equal(FakeOperator.actions, 0);
  } finally {
    globalThis.fetch = originalFetch;
    t.mock.timers.reset();
  }
});
