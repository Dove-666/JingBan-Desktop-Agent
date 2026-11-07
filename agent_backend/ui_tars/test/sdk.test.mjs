import { fixtureVerified } from './fixture-goal.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { GUIAgent } from '@ui-tars/sdk';
import { Jimp } from 'jimp';
import { createSdkAgent, isModernGptModel, modernGptFetch } from '../sdk-adapter.mjs';
import { BridgeController } from '../bridge.mjs';
import { runSyntheticProbe } from '../synthetic-model-probe.mjs';

// Synthetic pixels only. No desktop screenshot or native input is used.
const PNG = (await new Jimp({ width: 64, height: 64, color: 0xffffffff }).getBuffer('image/png')).toString('base64');
class FakeOperator {
  static MANUAL = { ACTION_SPACES: ["click(start_box='[x1,y1,x2,y2]')", 'finished()', 'call_user()'] };
  async screenshot() { return { base64: PNG, scaleFactor: 1 }; }
  async execute(params) { FakeOperator.actions.push(params.parsedPrediction.action_type); }
  static actions = [];
}

async function serverFor(callback) {
  const server = createServer(callback); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { url: `http://127.0.0.1:${server.address().port}/v1`,
    async close() { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); } };
}

function completion(response, content) {
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ id: 'fake-completion', object: 'chat.completion', created: 1, model: 'fake',
    choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content } }],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
}

function args(url, onEvent, signal = new AbortController().signal, maxSteps = 3) {
  return { config: { baseURL: url, apiKey: 'mock-key-only', model: 'fake', provider: 'ui-tars-1.5' },
    options: { maxSteps, timeoutMs: 1000 }, signal, onEvent };
}

test('real SDK performs local HTTP, parses UI-TARS actions, and finishes using only fake operator', async () => {
  FakeOperator.actions = []; let calls = 0; let key;
  const local = await serverFor(async (request, response) => {
    key = request.headers.authorization;
    for await (const _chunk of request) { /* drain only */ }
    completion(response, ++calls === 1 ? "Thought: test\nAction: click(start_box='(100,200)')" : 'Thought: test\nAction: finished()');
  });
  try {
    const events = []; const handle = await createSdkAgent(args(local.url, (e) => events.push(e)), { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('test synthetic window')).status, 'completed');
    assert.equal(key, 'Bearer mock-key-only');
    assert.equal(calls, 2);
    assert.deepEqual(FakeOperator.actions, ['click']);
    assert.deepEqual(events.filter((e) => e.stage === 'acting' && e.status === 'completed').map((e) => e.action), ['click', 'finished']);
  } finally { await local.close(); }
});

test('modern model matching follows the final model segment without changing unrelated models', async () => {
  for (const model of ['o1', 'openai/o3-mini', 'o4-mini', 'gpt-5', 'gpt-5.4', 'gateway/GPT-6.1']) assert.ok(isModernGptModel(model));
  for (const model of ['ui-tars-1.5-7b', 'doubao-1.5-ui-tars', 'gpt-4.1', 'gpt-50', 'gpt-5codex', 'foo/o30']) assert.ok(!isModernGptModel(model));
  const controller = new AbortController(); const init = { body: JSON.stringify({ max_tokens: 12 }), signal: controller.signal };
  let captured;
  await modernGptFetch((_input, next) => { captured = next; return Promise.resolve('ok'); })('http://localhost/v1/chat/completions', init);
  assert.equal(captured.signal, controller.signal);
  assert.ok(!Object.hasOwn(JSON.parse(captured.body), 'max_tokens'));
  assert.ok(!Object.hasOwn(JSON.parse(captured.body), 'max_completion_tokens'));
  await modernGptFetch((_input, next) => { captured = next; return Promise.resolve('ok'); })('http://localhost/v1/responses', init);
  assert.equal(captured, init);
});

test('real SDK modern GPT transport preserves model/auth and strips only incompatible body fields', async () => {
  const bodies = []; const keys = [];
  const local = await serverFor(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8'))); keys.push(request.headers.authorization);
    completion(response, 'Thought: test\nAction: finished()');
  });
  try {
    for (const model of ['gateway/gpt-6.1', 'UI-TARS-1.5-7B']) {
      const request = args(local.url, () => {}); request.config.model = model;
      const handle = await createSdkAgent(request, { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
      assert.equal((await handle.run('synthetic transport')).status, 'completed');
    }
    assert.equal(bodies[0].model, 'gateway/gpt-6.1');
    assert.ok(!Object.hasOwn(bodies[0], 'max_completion_tokens')); assert.equal(bodies[0].reasoning_effort, undefined);
    for (const name of ['max_tokens', 'temperature', 'top_p', 'seed', 'stop', 'frequency_penalty', 'presence_penalty', 'thinking']) assert.ok(!Object.hasOwn(bodies[0], name));
    assert.equal(bodies[1].model, 'UI-TARS-1.5-7B'); assert.ok(!Object.hasOwn(bodies[1], 'max_tokens'));
    assert.equal(bodies[1].temperature, 0); assert.equal(bodies[1].top_p, 0.7);
    assert.deepEqual(bodies[1].thinking, { type: 'disabled' });
    assert.ok(!Object.hasOwn(bodies[1], 'max_completion_tokens')); assert.ok(!Object.hasOwn(bodies[1], 'reasoning_effort'));
    assert.deepEqual(keys, ['Bearer mock-key-only', 'Bearer mock-key-only']);
  } finally { await local.close(); }
});

test('real SDK abort cancels model fetch and prevents native action after cancellation', async () => {
  FakeOperator.actions = []; let gotRequest;
  const received = new Promise((resolve) => { gotRequest = resolve; });
  const local = await serverFor((request, _response) => { request.resume(); gotRequest(); });
  try {
    const controller = new AbortController();
    const request = args(local.url, () => {}, controller.signal); request.config.model = 'openai/o3-mini';
    const handle = await createSdkAgent(request, { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    const pending = handle.run('test cancellation'); await received; controller.abort(); handle.stop();
    assert.equal((await pending).status, 'cancelled');
    assert.deepEqual(FakeOperator.actions, []);
  } finally { await local.close(); }
});

test('modern SDK HTTP errors cannot leak endpoint credentials or trigger retries', async () => {
  let calls = 0; const events = [];
  const local = await serverFor((request, response) => {
    calls++; request.resume(); response.writeHead(401, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: { message: 'mock-key-only', type: 'authentication_error' } }));
  });
  try {
    const bridge = new BridgeController({ emit: (event) => events.push(event),
      createAgent: (request) => createSdkAgent(request, { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified }) });
    const complete = new Promise((resolve) => {
      bridge.emit = (event) => { events.push(event); if (event.type === 'error' || event.type === 'final') resolve(); };
    });
    bridge.handle({ type: 'run', id: 'modern-error', task: 'synthetic',
      config: { baseURL: local.url, apiKey: 'mock-key-only', model: 'gpt-6.1', provider: 'ui-tars-1.5' } });
    await complete; assert.equal(calls, 1);
    // Upstream wraps an exhausted HTTP error as UNKNOWN_ERROR; retain our safe generic code.
    assert.equal(events.at(-1).code, 'SDK_FAILED');
    assert.ok(!JSON.stringify(events).includes('mock-key-only'));
  } finally { await local.close(); }
});

test('synthetic model probe proves parsed target hit and two-step finish without a native operator', async () => {
  let calls = 0;
  const local = await serverFor(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    assert.ok(!Object.hasOwn(body, 'max_completion_tokens'));
    assert.ok(!Object.hasOwn(body, 'max_tokens'));
    assert.equal(body.reasoning_effort, undefined);
    completion(response, ++calls === 1 ? "Thought: secret mock-key-only\nAction: click(start_box='(630,308)')" : 'Thought: done\nAction: finished()');
  });
  try {
    const result = await runSyntheticProbe({ config: { baseURL: local.url, apiKey: 'mock-key-only', model: 'gpt-6.1', provider: 'ui-tars-1.5' } });
    assert.equal(result.status, 'passed'); assert.equal(calls, 2); assert.equal(result.steps, 2);
    assert.deepEqual(result.parsedPoint, { x: 630, y: 308 });
    assert.equal(result.desktopScreenshots, 0); assert.equal(result.physicalInputActions, 0);
    assert.ok(!JSON.stringify(result).includes('mock-key-only'));
  } finally { await local.close(); }
});

test('synthetic probe rejects a wrong parsed coordinate instead of reporting format-only success', async () => {
  let calls = 0;
  const local = await serverFor((request, response) => {
    request.resume(); calls++; completion(response, "Thought: test\nAction: click(start_box='(210,308)')");
  });
  try {
    const result = await runSyntheticProbe({ config: { baseURL: local.url, apiKey: 'mock-key-only', model: 'gpt-6.1', provider: 'ui-tars-1.5' } });
    assert.equal(result.status, 'failed'); assert.equal(result.code, 'TARGET_MISSED');
    assert.equal(calls, 1); assert.equal(result.physicalInputActions, 0);
  } finally { await local.close(); }
});

test('synthetic CLI writes only one sanitized JSON result and uses credentials through stdin', async () => {
  let calls = 0;
  const local = await serverFor((request, response) => {
    request.resume(); completion(response, ++calls === 1 ? "Thought: mock-key-only\nAction: click(start_box='(630,308)')" : 'Thought: mock-key-only\nAction: finished()');
  });
  try {
    const child = spawn(process.execPath, [fileURLToPath(new URL('../synthetic-model-probe.mjs', import.meta.url))],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    let stdout = ''; let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    const closed = new Promise((resolve, reject) => { child.on('exit', (code) => resolve(code)); child.on('error', reject); });
    child.stdin.end(JSON.stringify({ config: { baseURL: local.url, apiKey: 'mock-key-only', model: 'gpt-6.1', provider: 'ui-tars-1.5' } }));
    assert.equal(await closed, 0); assert.equal(calls, 2); assert.equal(stderr, '');
    const lines = stdout.trim().split('\n'); assert.equal(lines.length, 1);
    assert.equal(JSON.parse(lines[0]).status, 'passed');
    assert.ok(!stdout.includes('mock-key-only')); assert.ok(!stdout.includes('Thought:'));
  } finally { await local.close(); }
});

test('real SDK reports step limit instead of success, and call_user becomes needs_user', async () => {
  const local = await serverFor((request, response) => { request.resume(); completion(response, "Thought: test\nAction: click(start_box='(100,200)')"); });
  try {
    const handle = await createSdkAgent(args(local.url, () => {}, undefined, 1), { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    await assert.rejects(handle.run('test step bound'), (exception) => exception.code === 'STEP_LIMIT');
  } finally { await local.close(); }
  const help = await serverFor((request, response) => { request.resume(); completion(response, 'Thought: test\nAction: call_user()'); });
  try {
    const handle = await createSdkAgent(args(help.url, () => {}), { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('test user help')).status, 'needs_user');
  } finally { await help.close(); }
});
