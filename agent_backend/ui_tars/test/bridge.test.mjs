import { verificationReport } from '../goal-verifier.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { BridgeController, validateRun, PROVIDERS } from '../bridge.mjs';

const request = (overrides = {}) => ({ type: 'run', id: 'task-1', task: 'Open a test window',
  config: { baseURL: 'http://127.0.0.1:9001/v1', apiKey: 'test-secret-123', model: 'test', provider: 'ui-tars-1.5' },
  options: { maxSteps: 3, timeoutMs: 1000 }, ...overrides });
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('validates provider formats, limits, URL, and snakecase credentials without expanding config', () => {
  const run = validateRun(request({ config: { base_url: 'http://localhost/v1', api_key: '', model: 'local' } }));
  assert.equal(run.config.provider, 'ui-tars-1.5');
  assert.equal(run.config.apiKey, '');
  assert.equal(run.options.maxSteps, 3);
  assert.equal(validateRun(request({ options: {} })).options.maxSteps, 0);
  assert.equal(validateRun(request({ options: { maxSteps: 0 } })).options.maxSteps, 0);
  assert.equal(validateRun(request({ options: { max_steps: 0 } })).options.maxSteps, 0);
  assert.equal(validateRun(request({ options: { maxSteps: 1000 } })).options.maxSteps, 1000);
  for (const provider of Object.keys(PROVIDERS)) assert.equal(validateRun(request({ config: { ...request().config, provider } })).config.provider, provider);
  for (const config of [{ ...request().config, provider: 'anthropic' }, { ...request().config, baseURL: 'file:///secret' },
    { ...request().config, baseURL: 'https://u:secret@example.com' }, { ...request().config, baseURL: 'https://example.com?token=secret' },
    { ...request().config, apiKey: 'key\nheader' }]) assert.throws(() => validateRun(request({ config })));
  for (const maxSteps of [-1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity, '0']) {
    assert.throws(() => validateRun(request({ options: { maxSteps } })));
  }
  assert.throws(() => validateRun(request({ options: { timeoutMs: 999 } })));
});

test('lazy ready never creates an SDK, and a run is exclusive until settled', async () => {
  const events = []; let resolveRun; let creates = 0;
  const bridge = new BridgeController({ emit: (event) => events.push(event), createAgent: async ({ onEvent }) => {
    creates++; onEvent({ stage: 'observing', step: 1, secret: 'MUST_NOT_PASS' });
    return { run: () => new Promise((resolve) => { resolveRun = resolve; }), stop() {} };
  } });
  bridge.ready(); bridge.handle({ type: 'ping', id: 'probe' });
  assert.equal(creates, 0);
  bridge.handle(request()); await tick();
  bridge.handle(request({ id: 'other' }));
  assert.equal(events.at(-1).code, 'BUSY');
  resolveRun({ status: 'completed', summary: 'done test-secret-123', verification: verificationReport('verified', 'GOAL_STATE_CONFIRMED') }); await tick();
  assert.equal(events.at(-1).status, 'completed');
  assert.equal(events.at(-1).summary, 'done [redacted]');
  assert.equal(events.at(-1).steps, 1);
  assert.equal(bridge.active, null);
  assert.ok(!JSON.stringify(events).includes('MUST_NOT_PASS'));
  assert.ok(!JSON.stringify(events).includes('test-secret-123'));
});

test('abort keeps task exclusive, forwards signal, and emits exactly one cancelled terminal', async () => {
  const events = []; let signal; let resolveRun; let stopped = 0;
  const bridge = new BridgeController({ emit: (event) => events.push(event), createAgent: async (args) => {
    signal = args.signal;
    return { run: () => new Promise((resolve) => { resolveRun = resolve; }), stop() { stopped++; } };
  } });
  bridge.handle(request()); await tick();
  bridge.handle({ type: 'abort', id: 'task-1' });
  assert.ok(signal.aborted); assert.equal(stopped, 1);
  bridge.handle(request({ id: 'other' })); assert.equal(events.at(-1).code, 'BUSY');
  resolveRun({ status: 'completed' }); await tick();
  assert.equal(events.filter((e) => e.type === 'final').length, 1);
  assert.equal(events.at(-1).status, 'cancelled');
});

test('timeout is terminal timed_out even when upstream throws secret-bearing error', async () => {
  const events = []; let fireTimeout; let rejectRun;
  const bridge = new BridgeController({ emit: (event) => events.push(event),
    setTimer: (callback) => { fireTimeout = callback; return 1; }, clearTimer() {},
    createAgent: async () => ({ run: () => new Promise((_resolve, reject) => { rejectRun = reject; }), stop() {} }) });
  bridge.handle(request()); await tick(); fireTimeout();
  rejectRun(new Error('apiKey=test-secret-123')); await tick();
  assert.equal(events.at(-1).status, 'timed_out');
  assert.ok(!JSON.stringify(events).includes('test-secret-123'));
});

test('raw SDK errors are never copied into protocol, and unknown result is not success', async () => {
  const events = [];
  const bridge = new BridgeController({ emit: (event) => events.push(event), createAgent: async () => {
    throw new Error('SDK model config apiKey=test-secret-123');
  } });
  bridge.handle(request()); await tick();
  assert.equal(events.at(-1).code, 'SDK_FAILED');
  assert.ok(!JSON.stringify(events).includes('test-secret-123'));
  const another = new BridgeController({ emit: (event) => events.push(event),
    createAgent: async () => ({ run: async () => ({ status: 'running' }) }) });
  another.handle(request()); await tick(); assert.equal(events.at(-1).type, 'error');
});

test('completed model summary is returned whole beyond 2000 characters with credentials redacted', async () => {
  const events = []; const summary = '完整完成摘要。'.repeat(600) + ' test-secret-123';
  const bridge = new BridgeController({ emit: (event) => events.push(event),
    createAgent: async () => ({ async run() { return { status: 'completed', summary, verification: verificationReport('verified', 'GOAL_STATE_CONFIRMED') }; } }) });
  bridge.handle(request()); await tick();
  const final = events.at(-1);
  assert.equal(final.type, 'final');
  assert.equal(final.status, 'completed');
  assert.equal(final.summary, summary.replace('test-secret-123', '[redacted]'));
  assert.ok(final.summary.length > 2000);
});

test('worker protocol accepts chunked UTF8, rejects invalid JSON regardless of size, and stays lazy for ping', async () => {
  const child = spawn(process.execPath, [new URL('../worker.mjs', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  let stdout = ''; let stderr = '';
  child.stdout.on('data', (data) => { stdout += data; });
  child.stderr.on('data', (data) => { stderr += data; });
  const closed = new Promise((resolve, reject) => { child.on('exit', (code) => resolve(code)); child.on('error', reject); });
  const bytes = Buffer.from(JSON.stringify({ type: 'ping', id: 'probe' }) + '\n');
  child.stdin.write(bytes.subarray(0, 7)); child.stdin.write(bytes.subarray(7));
  const longReference = Buffer.from(JSON.stringify({ type: 'ping', id: 'large-valid-json',
    reference_context: '完整中文参考'.repeat(30000) }) + '\n');
  assert.ok(longReference.length > 128 * 1024);
  child.stdin.write(longReference.subarray(0, 113)); child.stdin.write(longReference.subarray(113));
  child.stdin.write('x'.repeat(140 * 1024) + '\n');
  child.stdin.end('{invalid json}\n');
  assert.equal(await closed, 0);
  assert.equal(stderr, '');
  const events = stdout.trim().split('\n').map((line) => JSON.parse(line));
  assert.equal(events[0].type, 'ready');
  assert.equal(events[1].id, 'probe');
  assert.equal(events[2].id, 'large-valid-json');
  assert.equal(events[2].type, 'ready');
  assert.equal(events.filter((e) => e.code === 'INVALID_REQUEST').length, 2);
});
