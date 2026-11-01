import { fixtureVerified } from './fixture-goal.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { GUIAgent } from '@ui-tars/sdk';
import { parseBoxToScreenCoords } from '@ui-tars/sdk/core';
import { actionParser } from '@ui-tars/action-parser';
import { Jimp } from 'jimp';
import { parserImageSize, normalizeWindowsHotkey } from '../pixel-profile.mjs';
import { createSdkAgent } from '../sdk-adapter.mjs';
import { BridgeController, validateRun } from '../bridge.mjs';

test('explicit pixel profile validates independently from model names and defaults to native', () => {
  const request = { type: 'run', id: 'test', task: 'test', config: { baseURL: 'http://localhost/v1', model: 'arbitrary-alias' } };
  assert.equal(validateRun(request).config.coordinateProfile, 'native');
  assert.equal(validateRun({ ...request, config: { ...request.config, coordinate_profile: 'pixel' } }).config.coordinateProfile, 'pixel');
  assert.throws(() => validateRun({ ...request, config: { ...request.config, coordinateProfile: 'pixel', provider: 'ui-tars-1.0' } }));
  assert.throws(() => validateRun({ ...request, config: { ...request.config, coordinateProfile: 'unknown' } }));
  assert.throws(() => validateRun({ ...request, options: { diagnostics: 'true' } }));
});

test('pixel image dimensions match real pinned 1.5 parser at ordinary, small and large resolutions', () => {
  for (const [width, height] of [[1024, 768], [1920, 1080], [2560, 1440], [64, 64], [8000, 6000]]) {
    const size = parserImageSize(width, height);
    const parsed = actionParser({ prediction: `Action: click(start_box='(${size.width / 2},${size.height / 2})')`,
      factor: [1000, 1000], screenContext: { width, height }, scaleFactor: 1, modelVer: '1.5' }).parsed[0];
    const point = parseBoxToScreenCoords({ boxStr: parsed.action_inputs.start_box, screenWidth: width, screenHeight: height });
    assert.deepEqual(point, { x: width / 2, y: height / 2 });
  }
});

test('Windows hotkey aliases normalize completely and invalid chords cannot silently lose keys', () => {
  assert.equal(normalizeWindowsHotkey('Windows + D'), 'win+d');
  assert.equal(normalizeWindowsHotkey('super d'), 'win+d');
  assert.equal(normalizeWindowsHotkey('control + esc'), 'ctrl+escape');
  assert.equal(normalizeWindowsHotkey('Alt+F4'), 'alt+f4');
  assert.equal(normalizeWindowsHotkey('win+unknown'), null);
  assert.equal(normalizeWindowsHotkey(''), null);
});

const PNG = (await new Jimp({ width: 1024, height: 768, color: 0xffffffff }).getBuffer('image/png')).toString('base64');
class FakeOperator {
  static MANUAL = { ACTION_SPACES: ["click(start_box='(x,y)')", "hotkey(key='')", 'finished()'] };
  static actions = [];
  async screenshot() { return { base64: PNG, scaleFactor: 1 }; }
  async execute(params) { FakeOperator.actions.push(params); }
}
async function localModel(responses, received) {
  let calls = 0;
  const server = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks)); await received?.(body);
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ id: 'test', object: 'chat.completion', created: 1, model: 'alias',
      choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: responses[calls++] ?? 'Action: finished()' } }] }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { url: `http://127.0.0.1:${server.address().port}/v1`,
    async close() { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); } };
}
function args(url, coordinateProfile = 'pixel', diagnostics = false) {
  return { config: { baseURL: url, apiKey: 'test-private-key', model: 'arbitrary-alias', provider: 'ui-tars-1.5', coordinateProfile },
    options: { maxSteps: 3, timeoutMs: 10000, diagnostics }, signal: new AbortController().signal, onEvent() {} };
}

test('real SDK pixel profile sends aligned images, states Windows coordinates, and clicks original-screen center', async () => {
  FakeOperator.actions = []; const events = []; const bodies = [];
  const local = await localModel(["Thought: test-private-key hidden thought\nAction: click(start_box='(518,378)')", 'Action: finished()'], (body) => bodies.push(body));
  try {
    const request = args(local.url, 'pixel', true); request.onEvent = (event) => events.push(event);
    const handle = await createSdkAgent(request, { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('private task')).status, 'completed');
    const imageMessage = bodies[0].messages.find((message) => Array.isArray(message.content));
    const sent = await Jimp.read(Buffer.from(imageMessage.content[0].image_url.url.split(',')[1], 'base64'));
    assert.deepEqual([sent.bitmap.width, sent.bitmap.height], [1036, 756]);
    assert.match(bodies[0].messages[0].content, /real Windows desktop/);
    assert.match(bodies[0].messages.at(-1).content, /1036 pixels wide and 756 pixels high/);
    const params = FakeOperator.actions[0];
    assert.deepEqual(parseBoxToScreenCoords({ boxStr: params.parsedPrediction.action_inputs.start_box,
      screenWidth: params.screenWidth, screenHeight: params.screenHeight }), { x: 512, y: 384 });
    assert.deepEqual(events.find((event) => event.action === 'click' && event.status === 'completed').point, { x: 512, y: 384 });
    assert.ok(!JSON.stringify(events).includes('test-private-key')); assert.ok(!JSON.stringify(events).includes('private task'));
    assert.ok(!JSON.stringify(events).includes('hidden thought'));
  } finally { await local.close(); }
});

test('native profile leaves image dimensions, SDK prompt and hotkey behavior unchanged', async () => {
  FakeOperator.actions = []; const bodies = []; const events = [];
  const local = await localModel(["Action: hotkey(key='super+d')", 'Action: finished()'], (body) => bodies.push(body));
  try {
    const request = args(local.url, 'native'); request.config.model = 'UI-TARS-1.5-7B'; request.onEvent = (event) => events.push(event);
    const handle = await createSdkAgent(request, { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('test')).status, 'completed');
    const sent = await Jimp.read(Buffer.from(bodies[0].messages.find((m) => Array.isArray(m.content)).content[0].image_url.url.split(',')[1], 'base64'));
    assert.deepEqual([sent.bitmap.width, sent.bitmap.height], [1024, 768]);
    assert.ok(!bodies[0].messages[0].content.includes('real Windows desktop'));
    assert.equal(FakeOperator.actions[0].parsedPrediction.action_inputs.key, 'super+d');
    assert.ok(events.every((event) => !Object.hasOwn(event, 'point')));
  } finally { await local.close(); }
});

test('pixel profile normalizes hotkeys and prevents out-of-screen inputs before dispatch', async () => {
  FakeOperator.actions = [];
  const local = await localModel(["Action: hotkey(key='Windows+D')", "Action: click(start_box='(5000,5000)')"]);
  try {
    const handle = await createSdkAgent(args(local.url), { GUIAgent, NutJSOperator: FakeOperator, verifyGoal: fixtureVerified });
    await assert.rejects(handle.run('test'), (error) => error.code === 'ACTION_FAILED');
    assert.equal(FakeOperator.actions.length, 1);
    assert.equal(FakeOperator.actions[0].parsedPrediction.action_inputs.key, 'win+d');
  } finally { await local.close(); }
});

test('diagnostic protocol exposes only explicitly enabled bounded coordinates', async () => {
  const events = []; const request = { type: 'run', id: 'diagnostic', task: 'private task',
    config: { baseURL: 'http://localhost/v1', apiKey: 'private-key', model: 'alias' }, options: { diagnostics: true } };
  const bridge = new BridgeController({ emit: (event) => events.push(event), createAgent: async ({ onEvent }) => {
    onEvent({ stage: 'acting', step: 1, action: 'click', point: { x: 12, y: 34, secret: 'private-key' }, thought: 'hidden thought' });
    return { async run() { return { status: 'completed', summary: 'done' }; } };
  } });
  bridge.handle(request); await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events.find((event) => event.action === 'click').point, { x: 12, y: 34 });
  assert.ok(!JSON.stringify(events).includes('private-key')); assert.ok(!JSON.stringify(events).includes('private task'));
  assert.ok(!JSON.stringify(events).includes('hidden thought'));
  events.length = 0; bridge.handle({ ...request, options: {} }); await new Promise((resolve) => setImmediate(resolve));
  assert.ok(!Object.hasOwn(events.find((event) => event.action === 'click'), 'point'));
});
