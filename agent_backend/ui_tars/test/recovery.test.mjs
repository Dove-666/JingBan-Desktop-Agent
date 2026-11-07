/** Real SDK/localhost plus synthetic images; no desktop or production API. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { GUIAgent } from '@ui-tars/sdk';
import { IMAGE_PLACEHOLDER } from '@ui-tars/shared/constants';
import { Jimp } from 'jimp';
import { TaskModelContext } from '../model-context.mjs';
import { ProgressMonitor } from '../progress-monitor.mjs';
import { createNativeActionExecutor, createSdkAgent, modelDefaultFetch, modernGptFetch } from '../sdk-adapter.mjs';
import { parseVerdictDetailed, sanitizeFeedback, verificationReport } from '../goal-verifier.mjs';
import { BridgeController } from '../bridge.mjs';
import { fixtureVerified } from './fixture-goal.mjs';

const PNG = (await new Jimp({ width: 48, height: 48, color: 0xffffffff }).getBuffer('image/png')).toString('base64');
const completion = (response, content) => { response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ choices: [{ finish_reason: 'stop', message: { role: 'assistant', content } }] })); };
async function server(callback) {
  const bodies = [];
  const http = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks)); bodies.push(body);
    try { callback(body, response); } catch { response.writeHead(500); response.end(); }
  });
  http.listen(0, '127.0.0.1'); await once(http, 'listening');
  return { bodies, url: `http://127.0.0.1:${http.address().port}/v1`, async close() {
    http.closeAllConnections(); await new Promise(resolve => http.close(resolve)); } };
}
class Operator {
  static MANUAL = { ACTION_SPACES: ["click(start_box='(x,y)')", 'finished()'] };
  static actions = 0;
  async screenshot() { return { base64: PNG, scaleFactor: 1 }; }
  async execute() { Operator.actions++; }
}
const input = url => ({ config: { baseURL: url, apiKey: 'synthetic-key', model: 'fixture', provider: 'ui-tars-1.5' },
  options: { maxSteps: 0, timeoutMs: 0 }, signal: new AbortController().signal, onEvent() {} });

test('a thousand observations retain a bounded archive and target/current working set with visible gaps', () => {
  const context = new TaskModelContext(); context.begin('exact parent task');
  for (let index = 0; index < 1000; index++) context.capture([
    { from: 'human', value: IMAGE_PLACEHOLDER, screenshotBase64: `frame-${index}` },
    { from: 'gpt', value: `Action: scroll(direction='down') ${index}` }]);
  const projection = context.restore({ conversations: [{ from: 'human', value: 'exact parent task' }] });
  assert.ok(context.entries.length <= 512);
  assert.ok(context.entries.filter(entry => entry.screenshotBase64).length <= 24);
  assert.equal(projection.images.length, 8);
  assert.equal(projection.images[0], 'frame-0');
  assert.equal(projection.images.at(-1), 'frame-999');
  assert.equal(projection.conversations[0].value, 'exact parent task');
  assert.ok(projection.conversations.some(entry => entry.value.includes('HOST WORKING SET')));
  context.reduce(); assert.ok(context.restore({}).images.length <= 4);
});

test('no-progress detection concerns identical state/action, not overall iteration count', () => {
  const progress = new ProgressMonitor(); const action = { action_type: 'click', action_inputs: { start_box: '(0,0)' } };
  progress.observe('unchanged');
  assert.equal(progress.before(action).stalled, false);
  assert.equal(progress.before(action).stalled, false);
  assert.equal(progress.before(action).stalled, true);
  progress.observe('new actual state'); assert.equal(progress.before(action).stalled, false);
  for (let i = 0; i < 100; i++) { progress.observe('frame-' + i); assert.equal(progress.before(action).stalled, false); }
});

test('modern model compatibility preserves explicit reasoning preference', async () => {
  let captured;
  await modernGptFetch(async (_url, init) => { captured = JSON.parse(init.body); return {}; })('https://example.invalid/v1/chat/completions',
    { body: JSON.stringify({ reasoning_effort: 'high', temperature: 0.2 }) });
  assert.equal(captured.reasoning_effort, 'high'); assert.equal(captured.temperature, undefined);
});

test('temporary model HTTP failures get bounded request-only recovery; auth errors do not', async () => {
  let calls = 0; let cancelled = 0;
  const fetch = modelDefaultFetch(async () => (++calls === 1 ? { status: 503, body: { async cancel() { cancelled++; } } } : { status: 200 }));
  assert.equal((await fetch('https://example.invalid/v1/chat/completions', { body: '{}' })).status, 200);
  assert.equal(calls, 2); assert.equal(cancelled, 1);
  calls = 0;
  const denied = modelDefaultFetch(async () => { calls++; return { status: 401 }; });
  await denied('https://example.invalid/v1/chat/completions', { body: '{}' }); assert.equal(calls, 1);
});

test('native drag accepts zero edge coordinates and always releases the held button', async () => {
  const events = [];
  const native = { Point: class Point { constructor(x, y) { this.x = x; this.y = y; } },
    Button: { LEFT: 'left' }, straightTo: point => point, mouse: {
      async move(point) { events.push(['move', point.x, point.y]); },
      async pressButton(button) { events.push(['press', button]); },
      async releaseButton(button) { events.push(['release', button]); } } };
  const parse = ({ boxStr }) => { const [x, y] = boxStr.split(',').map(Number); return { x, y }; };
  const execute = createNativeActionExecutor(native, parse);
  await execute({ screenWidth: 100, screenHeight: 100,
    parsedPrediction: { action_type: 'drag', action_inputs: { start_box: '0,0', end_box: '0,50' } } }, new AbortController().signal);
  assert.deepEqual(events, [['move', 0, 0], ['press', 'left'], ['move', 0, 50], ['release', 'left']]);
  events.length = 0; let moved = 0;
  native.mouse.move = async () => { if (++moved === 2) throw new Error('synthetic move error'); };
  await assert.rejects(execute({ screenWidth: 100, screenHeight: 100,
    parsedPrediction: { action_type: 'drag', action_inputs: { start_box: '0,0', end_box: '50,50' } } }, new AbortController().signal));
  assert.deepEqual(events.at(-1), ['release', 'left']);
});

test('native horizontal scroll executes a supported method and unsupported directions fail explicitly', async () => {
  const calls = [];
  const execute = createNativeActionExecutor({ mouse: { async scrollRight(amount) { calls.push(amount); } } }, () => ({}));
  assert.equal(await execute({ screenWidth: 100, screenHeight: 100,
    parsedPrediction: { action_type: 'scroll', action_inputs: { direction: 'right' } } }, new AbortController().signal), true);
  assert.deepEqual(calls, [500]);
  await assert.rejects(execute({ parsedPrediction: { action_type: 'scroll', action_inputs: { direction: 'diagonal' } } },
    new AbortController().signal), error => error.code === 'UNSUPPORTED_ACTION');
});

test('invalid model action format is repaired before any operator input', async () => {
  Operator.actions = 0; let calls = 0;
  const local = await server((_body, response) => completion(response, ++calls === 1 ? 'I will inspect the window.' : 'Action: finished()'));
  try {
    const handle = await createSdkAgent(input(local.url), { GUIAgent, NutJSOperator: Operator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('inspect fixture')).status, 'completed');
    assert.equal(calls, 2); assert.equal(Operator.actions, 0);
    assert.ok(JSON.stringify(local.bodies[1]).includes('HOST FORMAT CORRECTION'));
  } finally { await local.close(); }
});

test('unchanged screen and repeated click return useful recovery feedback instead of unlimited input', async () => {
  Operator.actions = 0; let calls = 0;
  const local = await server((_body, response) => { calls++; completion(response, "Action: click(start_box='(140,140)')"); });
  try {
    const handle = await createSdkAgent(input(local.url), { GUIAgent, NutJSOperator: Operator });
    const result = await handle.run('open fixture');
    assert.equal(result.status, 'needs_user'); assert.equal(result.feedback.reason, 'NO_PROGRESS');
    assert.equal(Operator.actions, 2); assert.equal(calls, 3); assert.equal(result.effect_state, 'confirmed');
  } finally { await local.close(); }
});

test('context rejection retries only a genuinely smaller model request and never repeats input', async () => {
  Operator.actions = 0; let calls = 0; const sizes = [];
  const local = await server((body, response) => {
    sizes.push(body.messages.filter(message => Array.isArray(message.content)).length);
    if (++calls === 1) { response.writeHead(400, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: { code: 'context_length_exceeded', message: 'maximum context length' } })); }
    else completion(response, 'Action: finished()');
  });
  try {
    const run = input(local.url); run.priorEvidence = { frames: Array.from({ length: 8 }, (_, index) =>
      ({ base64: PNG, sequence: index + 1, captured_at: new Date().toISOString(), after_action: null })), summary: { total_observations: 8 } };
    const handle = await createSdkAgent(run, { GUIAgent, NutJSOperator: Operator, verifyGoal: fixtureVerified });
    assert.equal((await handle.run('inspect fixture')).status, 'completed');
    assert.deepEqual(sizes, [8, 4]); assert.equal(Operator.actions, 0);
  } finally { await local.close(); }
});

test('independent failed checks remain private bounded observations and do not become completion', () => {
  const value = parseVerdictDetailed(JSON.stringify({ outcome: 'failed', all_requirements_observed: true,
    target_identity_confirmed: true, temporal_requirements_observed: true, confidence: 'high',
    checks: [{ requirement: 'Save fixture', observed: 'No saved row; synthetic-key', passed: false }] }), 'synthetic-key');
  assert.equal(value.status, 'failed'); assert.equal(value.feedback.checks[0].passed, false);
  assert.ok(!JSON.stringify(value).includes('synthetic-key'));
  assert.equal(sanitizeFeedback({ reason: 'grant-permission', checks: [] }), undefined);
});

test('same-parent subgoals retain request-local evidence without leaking images into protocol', async () => {
  const events = []; const received = []; let count = 0;
  const frame = { base64: 'private-synthetic-pixels', sequence: 1, captured_at: new Date().toISOString() };
  let done;
  const bridge = new BridgeController({ emit(event) { events.push(event); if (event.type === 'final') done?.(); },
    createAgent: async request => { received.push(request); return { stop() {}, async run() { count++; return {
      status: 'completed', summary: 'done', verification: verificationReport('verified', 'GOAL_STATE_CONFIRMED'),
      __evidence: { frames: [frame], summary: { total_observations: count } } }; } }; } });
  const run = (subtask, task = 'parent task') => ({ type: 'run', id: `task:${count}`, task, subtask,
    config: { baseURL: 'http://127.0.0.1:9/v1', model: 'fixture' } });
  await new Promise(resolve => { done = resolve; bridge.handle(run('first subgoal')); });
  await new Promise(resolve => { done = resolve; bridge.handle(run('second subgoal', 'parent task; do not send anything')); });
  assert.equal(received[1].priorEvidence.frames[0].base64, frame.base64);
  assert.equal(received[1].task, 'parent task; do not send anything'); assert.equal(received[1].subtask, 'second subgoal');
  assert.ok(!JSON.stringify(events).includes('private-synthetic-pixels'));
  bridge.close(); assert.equal(bridge.sessionEvidence, null);
});

test('parent authorization stays exact while a narrower subgoal reaches action planner and judge', async () => {
  let judgeTask;
  const local = await server((_body, response) => completion(response, 'Action: finished()'));
  try {
    const request = input(local.url); request.subtask = 'Inspect fixture title only';
    const handle = await createSdkAgent(request, { GUIAgent, NutJSOperator: Operator,
      verifyGoal: async args => { judgeTask = args.task; return fixtureVerified(args); } });
    assert.equal((await handle.run('Inspect fixture title, then export a report with the parent planner')).status, 'completed');
    assert.equal(judgeTask, request.subtask);
    const instruction = local.bodies[0].messages[0].content;
    assert.ok(instruction.includes('Inspect fixture title, then export a report with the parent planner'));
    assert.ok(instruction.includes('CURRENT SUBGOAL'));
    assert.ok(instruction.includes('cannot add a new task or permission'));
  } finally { await local.close(); }
});

test('a quiet stale abort cannot stop a new subgoal or poison its protocol', async () => {
  const events=[];let signal,finish;let final;
  const done=new Promise(resolve=>{final=resolve;});
  const bridge=new BridgeController({emit(event){events.push(event);if(event.type==='final')final(event);},
    createAgent:async request=>{signal=request.signal;return{stop(){},run:()=>new Promise(resolve=>{finish=resolve;})};}});
  bridge.handle({type:'run',id:'parent:new-call',task:'parent',config:{baseURL:'http://127.0.0.1:9/v1',model:'fixture'}});
  await new Promise(resolve=>setImmediate(resolve));
  bridge.handle({type:'abort',id:'parent:old-call',quiet:true});
  assert.equal(signal.aborted,false);assert.equal(events.filter(event=>event.type==='error').length,0);
  bridge.handle({type:'abort',id:'parent:new-call',quiet:true});
  assert.equal(signal.aborted,true);finish({status:'cancelled',effect_state:'confirmed'});
  assert.equal((await done).status,'cancelled');
  bridge.handle({type:'abort',id:'parent:new-call',quiet:true});
  assert.equal(events.filter(event=>event.type==='error').length,0);
});
