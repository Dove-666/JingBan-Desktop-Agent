import test from 'node:test';
import assert from 'node:assert/strict';
import { parseVerdict, interpretVerdict, verifyVisualGoal } from '../goal-verifier.mjs';

const observed = () => ({ outcome: 'passed', all_requirements_observed: true,
  target_identity_confirmed: true, temporal_requirements_observed: true, confidence: 'high',
  checks: [{ requirement: '赞', observed: '红心', passed: true }] });

test('short Chinese evidence and complete JSON fences are supported without weakening state checks', () => {
  const raw = JSON.stringify(observed());
  for (const text of [raw, `\n\x60\x60\x60json\n${raw}\n\x60\x60\x60\n`, `\x60\x60\x60\n${raw}\n\x60\x60\x60`]) {
    assert.equal(parseVerdict(text).status, 'verified');
  }
  assert.equal(parseVerdict(JSON.stringify({ ...observed(), checks: [{ requirement: '赞', observed: '白心', passed: false }] })).status, 'failed');
});

test('extra prose, coercion, empty evidence and malformed fenced output never pass', () => {
  const raw = JSON.stringify(observed());
  for (const text of [`Success! ${raw}`, `\x60\x60\x60json\n${raw}\n\x60\x60\x60\nextra`,
    '```js\n' + raw + '\n```', raw + raw, 'null', '{}',
    JSON.stringify({ ...observed(), target_identity_confirmed: 'true' }),
    JSON.stringify({ ...observed(), checks: [{ requirement: '赞', observed: ' ', passed: true }] })]) {
    assert.notEqual(parseVerdict(text).status, 'verified');
  }
});

test('unknown results preserve identity and navigation evidence categories', () => {
  assert.equal(interpretVerdict({ ...observed(), outcome: 'unknown', target_identity_confirmed: false }).reason, 'TARGET_IDENTITY_UNCONFIRMED');
  assert.equal(interpretVerdict({ ...observed(), outcome: 'unknown', temporal_requirements_observed: false }).reason, 'TEMPORAL_EVIDENCE_MISSING');
  assert.equal(interpretVerdict({ ...observed(), outcome: 'unknown' }).reason, 'GOAL_NOT_OBSERVABLE');
});

test('timeline metadata is bounded and must not imply contiguous video positions', async () => {
  let sent;
  const result = await verifyVisualGoal({ task: '给第二个视频点赞', signal: new AbortController().signal,
    config: { baseURL: 'https://fixture.invalid/v1', model: 'test' },
    observations: [{ base64: 'first', sequence: 3, captured_at: '2026-11-01T15:00:00.000Z', after_action: 'scroll' },
      { base64: 'last', sequence: 30, after_action: 'private coordinates and typed text' }],
    async fetchImpl(_url, init) {
      sent = JSON.parse(init.body);
      return new Response(JSON.stringify({ choices: [{ finish_reason: 'stop', message: { content: JSON.stringify(observed()) } }] }));
    } });
  assert.equal(result.status, 'verified');
  assert.match(JSON.stringify(sent), /capture=30/);
  assert.match(JSON.stringify(sent), /possible gaps/);
  assert.ok(!JSON.stringify(sent).includes('private coordinates and typed text'));
});

test('oversized history is refused before a network request', async () => {
  const result = await verifyVisualGoal({ task: 'fixture', config: {}, signal: new AbortController().signal,
    observations: Array.from({ length: 9 }, () => ({ base64: 'fixture' })),
    async fetchImpl() { assert.fail('oversized history must not be sent'); } });
  assert.notEqual(result.status, 'verified');
});
