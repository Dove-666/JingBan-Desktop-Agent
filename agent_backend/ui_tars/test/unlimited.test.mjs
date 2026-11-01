import { fixtureVerified } from './fixture-goal.mjs';
/** Real SDK + local stub + synthetic frames only; no native import or input. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { GUIAgent } from '@ui-tars/sdk';
import { Jimp } from 'jimp';
import { createSdkAgent } from '../sdk-adapter.mjs';
import { BridgeController, validateRun } from '../bridge.mjs';

const frames = await Promise.all(Array.from({ length: 18 }, async (_, index) =>
  (await new Jimp({ width: 32, height: 32, color: ((index + 1) << 24 | 0x000000ff) >>> 0 }).getBuffer('image/png')).toString('base64')));

function fakeOperator() {
  return class FakeOperator {
    static MANUAL = { ACTION_SPACES: ["click(start_box='(x,y)')", 'finished()', 'call_user()'] };
    static screenshots = 0;
    static actions = 0;
    async screenshot() { return { base64: frames[FakeOperator.screenshots++], scaleFactor: 1 }; }
    async execute() { FakeOperator.actions++; }
  };
}

function completion(response, content) {
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ id: 'offline', object: 'chat.completion', created: 1, model: 'offline',
    choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content } }] }));
}

async function modelServer(callback) {
  const server = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    await callback(JSON.parse(Buffer.concat(chunks)), response);
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { url: `http://127.0.0.1:${server.address().port}/v1`,
    async close() { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); } };
}

function request(url) {
  return validateRun({ type: 'run', id: 'unlimited', task: 'offline synthetic task',
    config: { baseURL: url, apiKey: 'offline-key', model: 'offline', provider: 'ui-tars-1.5' },
    options: { timeoutMs: 0 } });
}

test('zero wire sentinel becomes actual SDK Infinity without falling back to its default', async () => {
  let received;
  class InspectAgent {
    constructor(config) { received = config.maxLoopCount; }
    stop() {}
  }
  const run = request('http://localhost/v1');
  assert.equal(run.options.maxSteps, 0);
  assert.equal(JSON.parse(JSON.stringify(run)).options.maxSteps, 0);
  await createSdkAgent({ ...run, signal: new AbortController().signal, onEvent() {} }, { GUIAgent: InspectAgent, NutJSOperator: fakeOperator() });
  assert.equal(received, Infinity);
});

test('unlimited real SDK completes after 17 fresh observations and returns latest pixels plus action history', async () => {
  const Operator = fakeOperator(); let calls = 0; const observed = [];
  const local = await modelServer(async (body, response) => {
    const step = ++calls;
    const images = body.messages.filter((message) => Array.isArray(message.content));
    const colors = [];
    for (const message of images) {
      const image = await Jimp.read(Buffer.from(message.content[0].image_url.url.split(',')[1], 'base64'));
      colors.push(image.bitmap.data[0]);
    }
    const actions = body.messages.filter((message) => message.role === 'assistant').map((message) => message.content);
    observed.push({ step, colors, actions });
    completion(response, step < 17 ? `Thought: synthetic step ${step}\nAction: click(start_box='(140,140)')` : 'Action: finished()');
  });
  try {
    const events = []; const handle = await createSdkAgent({ ...request(local.url), signal: new AbortController().signal,
      onEvent: (event) => events.push(event) }, { GUIAgent, NutJSOperator: Operator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('offline synthetic task')).status, 'completed');
    assert.equal(calls, 17); assert.equal(Operator.screenshots, 18); assert.equal(Operator.actions, 16);
    assert.equal(events.filter((event) => event.stage === 'observing').at(-1).step, 18);
    for (const { step, colors, actions } of observed) {
      assert.deepEqual(colors, Array.from({ length: step }, (_, index) => index + 1));
      assert.equal(actions.length, step - 1);
      if (step > 1) assert.match(actions.at(-1), new RegExp(`synthetic step ${step - 1}\\nAction: click`));
    }
  } finally { await local.close(); }
});

test('unlimited real SDK can be cancelled on its 13th model request before dispatching another action', async () => {
  const Operator = fakeOperator(); let calls = 0; let atThirteenth;
  const reached = new Promise((resolve) => { atThirteenth = resolve; });
  const local = await modelServer((_body, response) => {
    if (++calls === 13) { atThirteenth(); return; }
    completion(response, "Action: click(start_box='(140,140)')");
  });
  try {
    const controller = new AbortController();
    const handle = await createSdkAgent({ ...request(local.url), signal: controller.signal, onEvent() {} }, { GUIAgent, NutJSOperator: Operator, verifyGoal: fixtureVerified });
    const running = handle.run('offline synthetic task');
    await reached; controller.abort(); handle.stop();
    assert.equal((await running).status, 'cancelled');
    assert.equal(calls, 13); assert.equal(Operator.screenshots, 13); assert.equal(Operator.actions, 12);
  } finally { await local.close(); }
});

test('explicit finite BridgeController timeout still cancels real SDK and emits exactly one terminal', async () => {
  const Operator = fakeOperator(); let reached; let fireTimeout;
  const receiving = new Promise((resolve) => { reached = resolve; });
  const local = await modelServer(() => { reached(); });
  try {
    const events = []; let terminal;
    const done = new Promise((resolve) => { terminal = resolve; });
    const bridge = new BridgeController({ setTimer: (callback) => { fireTimeout = callback; return 1; }, clearTimer() {},
      createAgent: (run) => createSdkAgent(run, { GUIAgent, NutJSOperator: Operator, verifyGoal: fixtureVerified }),
      emit: (event) => { events.push(event); if (event.type === 'final' || event.type === 'error') terminal(event); } });
    bridge.handle({ type: 'run', ...request(local.url), options: { maxSteps: 0, timeoutMs: 1000 } });
    await receiving; fireTimeout();
    assert.equal((await done).status, 'timed_out');
    assert.equal(events.filter((event) => event.type === 'final' || event.type === 'error').length, 1);
    assert.equal(Operator.actions, 0);
  } finally { await local.close(); }
});

test('unlimited real SDK still stops at call_user', async () => {
  const Operator = fakeOperator(); let calls = 0;
  const local = await modelServer((_body, response) => { calls++; completion(response, 'Action: call_user()'); });
  try {
    const handle = await createSdkAgent({ ...request(local.url), signal: new AbortController().signal, onEvent() {} }, { GUIAgent, NutJSOperator: Operator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('offline synthetic task')).status, 'needs_user');
    assert.equal(calls, 1); assert.equal(Operator.actions, 0);
  } finally { await local.close(); }
});
