/** Real SDK + localhost judge + synthetic pixels, never user's desktop/API. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { GUIAgent } from '@ui-tars/sdk';
import { Jimp } from 'jimp';
import { createSdkAgent } from '../sdk-adapter.mjs';
import { BridgeController, validateRun } from '../bridge.mjs';
import { interpretVerdict, sanitizeVerification, verificationReport, verifyVisualGoal } from '../goal-verifier.mjs';

const images = await Promise.all([0xffffffff, 0xff0000ff, 0x00ff00ff].map(async color =>
  (await new Jimp({ width: 64, height: 64, color }).getBuffer('image/png')).toString('base64')));
const good = () => ({ outcome: 'passed', all_requirements_observed: true, target_identity_confirmed: true,
  temporal_requirements_observed: true, confidence: 'high', checks: [{ requirement: 'Toggle the fixture heart on', observed: 'The fixture heart is filled red', passed: true }] });
const isJudge = body => body.messages[0]?.content?.includes('independent read-only desktop RESULT verifier');
function completion(response, content) {
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ choices: [{ finish_reason: 'stop', message: { role: 'assistant', content } }] }));
}
async function localServer(callback) {
  const bodies = [];
  const server = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks)); bodies.push(body);
    try { await callback(body, response); } catch { response.writeHead(500); response.end(); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { bodies, url: `http://127.0.0.1:${server.address().port}/v1`,
    async close() { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}
function operator({ miss = false, failFinalCapture = false } = {}) {
  return class Operator {
    static MANUAL = { ACTION_SPACES: ["click(start_box='(x,y)')", 'finished()'] };
    static clicks = 0; static captures = 0;
    async screenshot() {
      if (++Operator.captures > 2 && failFinalCapture) throw new Error('secret private pixels');
      return { base64: !miss && Operator.clicks ? images[1] : images[0], scaleFactor: 1 };
    }
    async execute() { Operator.clicks++; }
  };
}
function input(url, signal = new AbortController().signal, options = {}) {
  return { config: { baseURL: url, apiKey: 'private-fixture-key', model: 'fake', provider: 'ui-tars-1.5' },
    options: { maxSteps: 3, timeoutMs: 0, ...options }, signal, onEvent() {} };
}

test('judge schema rejects missing evidence, partial success, low confidence and wrong target', () => {
  assert.equal(interpretVerdict(good()).status, 'verified');
  for (const patch of [{ outcome: 'unknown' }, { all_requirements_observed: false }, { target_identity_confirmed: false },
    { temporal_requirements_observed: false }, { confidence: 'medium' }, { checks: [] }, { checks: [{ requirement: 'task', observed: '', passed: true }] }]) {
    assert.notEqual(interpretVerdict({ ...good(), ...patch }).status, 'verified');
  }
  assert.equal(interpretVerdict({ ...good(), checks: [{ requirement: 'Like fixture', observed: 'Heart remains white', passed: false }] }).status, 'failed');
  assert.equal(sanitizeVerification({ ...verificationReport('verified', 'GOAL_STATE_CONFIRMED'), reason: 'private-fixture-key' }).status, 'unverified');
});

test('missed click and action model finished cannot yield completed; independent judge sees current pixels', async () => {
  const Operator = operator({ miss: true }); let actionCalls = 0; const events = [];
  const local = await localServer((body, response) => {
    if (isJudge(body)) {
      assert.equal(body.messages.length, 2);
      assert.ok(body.messages[1].content.filter(c => c.type === 'image_url').every(c => c.image_url.url.endsWith(images[0])));
      assert.ok(!JSON.stringify(body).includes('I definitely liked it'));
      return completion(response, JSON.stringify({ ...good(), outcome: 'failed', checks: [{ requirement: 'Like fixture', observed: 'Heart is still white', passed: false }] }));
    }
    completion(response, ++actionCalls === 1 ? "Action: click(start_box='(100,200)')" : 'Thought: I definitely liked it\nAction: finished()');
  });
  try {
    const run = input(local.url); run.onEvent = e => events.push(e);
    const handle = await createSdkAgent(run, { GUIAgent, NutJSOperator: Operator });
    const result = await handle.run('Turn on the fixture heart');
    assert.equal(result.status, 'needs_user'); assert.equal(result.verification.status, 'failed');
    assert.equal(Operator.clicks, 1); assert.equal(Operator.captures, 3);
    assert.equal(local.bodies.length, 3);
    assert.deepEqual(events.filter(e => e.stage === 'verifying').map(e => e.status), ['running', 'failed']);
    assert.ok(!JSON.stringify(result).includes('I definitely liked it'));
    assert.ok(!JSON.stringify(result).includes('private-fixture-key'));
  } finally { await local.close(); }
});

test('completed requires separate strict judge and new capture after native input', async () => {
  const Operator = operator(); let actionCalls = 0;
  const local = await localServer((body, response) => {
    if (isJudge(body)) {
      assert.equal(Operator.captures, 3); assert.equal(Operator.clicks, 1);
      assert.equal(body.messages[1].content.filter(c => c.type === 'image_url').at(-1).image_url.url, `data:image/png;base64,${images[1]}`);
      return completion(response, JSON.stringify(good()));
    }
    completion(response, ++actionCalls === 1 ? "Action: click(start_box='(100,200)')" : 'Action: finished()');
  });
  try {
    const result = await (await createSdkAgent(input(local.url), { GUIAgent, NutJSOperator: Operator })).run('Turn on the fixture heart');
    assert.equal(result.status, 'completed'); assert.equal(result.verification.status, 'verified');
    assert.ok(result.verification.checked_at); assert.equal(local.bodies.filter(isJudge).length, 1);
  } finally { await local.close(); }
});

test('malformed, unknown or unavailable verification never accepts model finished or repeats input', async () => {
  for (const verdict of ['Action: finished()', JSON.stringify({ ...good(), outcome: 'unknown' }), 'HTTP_ERROR']) {
    const Operator = operator(); let actions = 0;
    const local = await localServer((body, response) => {
      if (isJudge(body)) {
        if (verdict === 'HTTP_ERROR') { response.writeHead(503); response.end('private-fixture-key'); return; }
        return completion(response, verdict);
      }
      completion(response, ++actions === 1 ? "Action: click(start_box='(100,200)')" : 'Action: finished()');
    });
    try {
      const result = await (await createSdkAgent(input(local.url), { GUIAgent, NutJSOperator: Operator })).run('Turn on fixture heart');
      assert.equal(result.status, 'needs_user'); assert.equal(result.verification.status, 'unverified');
      assert.equal(Operator.clicks, 1); assert.ok(!JSON.stringify(result).includes('private-fixture-key'));
    } finally { await local.close(); }
  }
});

test('fresh final capture failure cannot reuse earlier image as a successful verification', async () => {
  const Operator = operator({ failFinalCapture: true }); let calls = 0;
  const local = await localServer((_body, response) => completion(response, ++calls === 1 ? "Action: click(start_box='(100,200)')" : 'Action: finished()'));
  try {
    const result = await (await createSdkAgent(input(local.url), { GUIAgent, NutJSOperator: Operator })).run('Turn on fixture heart');
    assert.equal(result.status, 'needs_user'); assert.equal(result.verification.reason, 'VERIFICATION_CAPTURE_FAILED');
    assert.equal(calls, 2); assert.equal(Operator.clicks, 1);
  } finally { await local.close(); }
});

test('multiple predicted actions use one input per new observation and trailing finished is discarded', async () => {
  const Operator = operator(); let calls = 0;
  const local = await localServer((body, response) => {
    if (isJudge(body)) return completion(response, JSON.stringify(good()));
    completion(response, ++calls === 1 ? "Action: click(start_box='(100,200)')\n\nclick(start_box='(300,400)')\n\nfinished()" : 'Action: finished()');
  });
  try {
    const result = await (await createSdkAgent(input(local.url), { GUIAgent, NutJSOperator: Operator })).run('Turn on fixture heart');
    assert.equal(result.status, 'completed'); assert.equal(calls, 2);
    assert.equal(Operator.clicks, 1); assert.equal(Operator.captures, 3);
  } finally { await local.close(); }
});

test('verifyOnly does no action/model planning and rejects ordinal targets without pre-action anchor', async () => {
  const Operator = operator();
  const local = await localServer((body, response) => { assert.ok(isJudge(body)); completion(response, JSON.stringify(good())); });
  try {
    const plain = await (await createSdkAgent(input(local.url, undefined, { verifyOnly: true }), { GUIAgent, NutJSOperator: Operator })).run('Turn on fixture heart');
    assert.equal(plain.status, 'completed'); assert.equal(Operator.clicks, 0); assert.equal(local.bodies.length, 1);
    const ordinal = await (await createSdkAgent(input(local.url, undefined, { verifyOnly: true }), { GUIAgent, NutJSOperator: Operator })).run('给首页第一个视频点赞');
    assert.equal(ordinal.status, 'needs_user'); assert.equal(ordinal.verification.reason, 'TEMPORAL_EVIDENCE_MISSING');
    assert.equal(local.bodies.length, 1); assert.equal(Operator.clicks, 0);
    assert.equal(validateRun({ type: 'run', id: 'verify', task: 'fixture', config: input(local.url).config, options: { verifyOnly: true } }).options.verifyOnly, true);
    assert.throws(() => validateRun({ type: 'run', id: 'verify', task: 'fixture', config: input(local.url).config, options: { verifyOnly: 'true' } }));
  } finally { await local.close(); }
});

test('Stop during judge request aborts it and never returns completed', async () => {
  const Operator = operator(); const controller = new AbortController(); let arrived;
  const judging = new Promise(resolve => { arrived = resolve; });
  const local = await localServer((body, response) => { if (isJudge(body)) { arrived(); return; } completion(response, 'Action: finished()'); });
  try {
    const handle = await createSdkAgent(input(local.url, controller.signal), { GUIAgent, NutJSOperator: Operator });
    const pending = handle.run('Observe the fixture'); await judging; controller.abort(); handle.stop();
    assert.equal((await pending).status, 'cancelled'); assert.equal(Operator.clicks, 0);
  } finally { await local.close(); }
});

test('bridge downgrades unverified upstream completed and strips unexpected report fields', async () => {
  for (const verification of [undefined, { status: 'verified', method: 'visual', reason: 'private-fixture-key', checked_at: new Date().toISOString() }]) {
    const events = []; const bridge = new BridgeController({ emit: e => events.push(e),
      createAgent: async () => ({ async run() { return { status: 'completed', summary: 'I definitely liked it', verification }; } }) });
    bridge.handle({ type: 'run', id: 'gate', task: 'fixture', config: input('http://localhost/v1').config });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(events.at(-1).status, 'needs_user'); assert.notEqual(events.at(-1).verification.status, 'verified');
    assert.ok(!JSON.stringify(events).includes('I definitely liked it')); assert.ok(!JSON.stringify(events).includes('private-fixture-key'));
  }
});

test('judge keeps bounded per-call timeout even for an unlimited whole task', async () => {
  const controller = new AbortController(); const local = await localServer(() => {});
  try {
    const report = await verifyVisualGoal({ task: 'Observe fixture', config: input(local.url).config,
      observations: [{ base64: images[0] }], signal: controller.signal, timeoutMs: 30 });
    assert.equal(report.status, 'unverified'); assert.equal(report.reason, 'VERIFICATION_SERVICE_UNAVAILABLE');
    assert.equal(controller.signal.aborted, false);
  } finally { await local.close(); }
});

test('second-video verification retains first-to-second navigation and checks final state instead of planner claims', async () => {
  // Deterministic video identities exist only in these synthetic fixture
  // frames. Production does not treat a changing image hash as video identity.
  const videoFrames = await Promise.all([0x333333ff, 0x1122aaff, 0x118899ff, 0xee1144ff].map(async color =>
    (await new Jimp({ width: 64, height: 64, color }).getBuffer('image/png')).toString('base64')));
  for (const miss of [false, true]) {
    let plannerCalls = 0; let received;
    class VideoOperator {
      static MANUAL = { ACTION_SPACES: ["click(start_box='(x,y)')", "scroll(direction='down')", 'finished()'] };
      static selected = 0; static liked = false; static captures = 0; static actions = [];
      async screenshot() {
        VideoOperator.captures++;
        const index = VideoOperator.selected === 0 ? 0 : VideoOperator.selected === 1 ? 1 : VideoOperator.liked ? 3 : 2;
        return { base64: videoFrames[index], scaleFactor: 1 };
      }
      async execute({ parsedPrediction: prediction }) {
        VideoOperator.actions.push(prediction.action_type);
        if (prediction.action_type === 'scroll') VideoOperator.selected = 2;
        else if (prediction.action_type === 'click' && VideoOperator.selected === 0) VideoOperator.selected = 1;
        else if (prediction.action_type === 'click' && VideoOperator.selected === 2 && !miss) VideoOperator.liked = true;
        else if (prediction.action_type !== 'click') throw new Error('Fixture expected one navigation action');
      }
    }
    const local = await localServer((body, response) => {
      if (isJudge(body)) {
        const visible = body.messages[1].content.filter(c => c.type === 'image_url').map(c => c.image_url.url.split(',')[1]);
        const first = visible.indexOf(videoFrames[1]); const second = visible.indexOf(videoFrames[2]);
        const identityObserved = first !== -1 && second > first;
        const stateObserved = visible.at(-1) === videoFrames[3];
        const verdict = identityObserved ? { ...good(), outcome: stateObserved ? 'passed' : 'failed',
          checks: [{ requirement: 'Like the second fixture video', observed: stateObserved ? 'Second target heart is filled' : 'Second target heart stays white', passed: stateObserved }] } :
          { ...good(), outcome: 'unknown', temporal_requirements_observed: false };
        return completion(response, JSON.stringify(verdict));
      }
      const actions = ["click(start_box='(20,20)')", "scroll(direction='down', start_box='(32,32)')", "click(start_box='(40,40)')", 'finished()'];
      completion(response, 'Action: ' + actions[plannerCalls++]);
    });
    try {
      const handle = await createSdkAgent(input(local.url, undefined, { maxSteps: 6 }), {
        GUIAgent, NutJSOperator: VideoOperator,
        async verifyGoal(parameters) { received = parameters; return verifyVisualGoal(parameters); },
      });
      const result = await handle.run('打开测试视频首页，给主页第二个视频点赞');
      assert.equal(result.status, miss ? 'needs_user' : 'completed');
      assert.deepEqual(VideoOperator.actions, ['click', 'scroll', 'click']);
      assert.equal(VideoOperator.captures, 5);
      assert.equal(plannerCalls, 4);
      assert.deepEqual(received.observations.map(frame => frame.sequence), [1, 2, 3, 4, 5]);
      assert.deepEqual(received.observations.map(frame => frame.after_action), [null, 'click', 'scroll', 'click', null]);
      assert.equal(received.observations.at(-1).base64, miss ? videoFrames[2] : videoFrames[3]);
      assert.equal(received.observationSummary.omitted_observations, 0);
      assert.equal(local.bodies.filter(isJudge).length, 1);
    } finally { await local.close(); }
  }
});
