import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve, join } from 'node:path';
import { approvalMode, approvalOutcome, automaticComputerAnswer, permissionPreset, toolArgumentsDetail } from '../approval.mjs';
import { askDecision, contained, mutationPaths } from '../approval-policy.mjs';
import { launchConfiguration } from '../configuration.mjs';
import { HarnessRunner } from '../runner.mjs';

const workspace = resolve('permission-fixture');
const context = { fs: { resolve: async (path, options = {}) => ({ targetKey: resolve(options.cwd ?? workspace, path) }) },
  workingDirectory: { get: () => workspace } };
const execution = (name, args = {}) => ({ name, arguments: args, signal: new AbortController().signal });

test('mode validation is closed and absent mode uses upstream Auto review', () => {
  assert.equal(approvalMode(), 'auto');
  for (const invalid of ['never', 'danger-full-access', '', null, true]) assert.throws(() => approvalMode(invalid));
  assert.deepEqual(['ask', 'auto', 'full'].map(permissionPreset), ['workspace-write', 'auto', 'danger-full-access']);
});

test('stale accepted text never overrides a cancelled, declined or expired response', () => {
  assert.equal(approvalOutcome({ text: '允许本次' }), 'allowed-once');
  for (const answer of [null, { text: '允许本次', expired: true }, { text: '允许本次', declined: true }]) {
    assert.equal(approvalOutcome(answer), 'cancelled');
  }
  assert.equal(approvalOutcome({ text: '允许本次' }, true), 'cancelled');
  assert.equal(approvalOutcome({ text: '允许本次 extra' }), 'rejected');
});

test('approval details decode actual SDK argument events exactly once', () => {
  const value = { file_path: 'C:\\owned\\outside.txt', content: 'fixture' };
  assert.equal(toolArgumentsDetail(JSON.stringify(value)), JSON.stringify(value, null, 2));
  assert.equal(toolArgumentsDetail(value), JSON.stringify(value, null, 2));
});

test('launch policy forces native execution and composes the real upstream reviewer', () => {
  for (const mode of ['ask', 'auto', 'full']) {
    const config = launchConfiguration({ type: 'run', id: 'fixture', task: 'test',
      options: { workspace, approval_mode: mode }, storage: { home: resolve('permission-home') },
      config: { base_url: 'http://127.0.0.1:1', model: 'fixture', api_key: 'synthetic-key' } });
    assert.equal(config.permissionPreset, permissionPreset(mode));
    assert.equal(config.patches.find(row => row.id === 'tool-executor-ptc').disabled, true);
    assert.equal(config.patches.find(row => row.id === 'approval').config.policy, mode === 'full' ? 'never' : 'ask');
    const inserts = config.patches.flatMap(row => row.insert ?? []);
    assert.ok(inserts.some(row => row.name.includes('dsh-experimental-auto-review')));
    assert.equal(inserts.some(row => row.id === 'jingban-approval-policy'), mode === 'ask');
  }
});

test('ask requires web, command and unknown tools before execution', async () => {
  for (const name of ['web_fetch', 'web_search', 'pwsh', 'bash', 'cua', 'workflow', 'future_tool']) {
    assert.equal((await askDecision(context, execution(name, { command: 'example' }), workspace)).kind, 'ask');
  }
  assert.equal((await askDecision(context, execution('read'), workspace)).kind, 'allow');
  assert.equal((await askDecision(context, execution('pwsh', { command: 'x'.repeat(13000) }), workspace)).kind, 'deny');
});

test('ask does not silently accept temp/outside writes and requires native single-call escalation', async () => {
  assert.equal((await askDecision(context, execution('write', { file_path: 'inside.txt' }), workspace)).kind, 'allow');
  assert.equal((await askDecision(context, execution('write', { file_path: '../outside.txt' }), workspace)).kind, 'deny');
  assert.equal((await askDecision(context, execution('write', { file_path: '../outside.txt',
    sandbox_permissions: 'danger-full-access', justification: 'Write the named report outside the task folder.' }), workspace)).kind, 'allow');
  const symlinkContext = { ...context, fs: { resolve: async path => ({ targetKey: path === workspace ? workspace : resolve('../external') }) } };
  assert.equal((await askDecision(symlinkContext, execution('edit', { file_path: 'link/file.txt' }), workspace)).kind, 'deny');
  assert.equal(contained(`${workspace}-sibling`, workspace), false);
  assert.equal(contained(join(workspace, 'file.txt'), workspace), true);
  const patch = execution('apply_patch', { input: '*** Begin Patch\n*** Update File: inside.txt\n*** Move to: ../outside.txt\n*** End Patch' });
  assert.deepEqual(mutationPaths(patch), ['inside.txt', '../outside.txt']);
  assert.equal((await askDecision(context, patch, workspace)).kind, 'deny');
});

test('full and reviewed Auto grant only official CUA access and never persistent consent', async () => {
  const question = { id: 'cua-app-access', question: 'Access application?', options: [
    { label: 'Allow for this session' }, { label: 'Deny' }, { label: 'Always allow' }],
    intent: { kind: 'approval', approve: 'Allow for this session', reject: 'Deny', always: 'Always allow' } };
  assert.equal(automaticComputerAnswer(question, 'ask'), null);
  for (const mode of ['auto', 'full']) {
    assert.deepEqual(automaticComputerAnswer(question, mode), { id: question.id, selected: ['Allow for this session'] });
    assert.equal(automaticComputerAnswer({ ...question, id: 'real-user-choice' }, mode), null);
    const runner = new HarnessRunner();
    runner.request = { id: 'fixture', options: { approval_mode: mode } };
    let questions = 0;
    runner.ask = async () => { questions++; return { text: 'blue' }; };
    const response = await runner.userQuestions({ questions: [question,
      { id: 'color', question: 'Which color?', options: [{ label: 'blue' }] }] }, { signal: new AbortController().signal });
    assert.equal(questions, 1);
    assert.deepEqual(response.answers[1], { id: 'color', selected: ['blue'] });
    runner.cancelled = true;
    assert.deepEqual(await runner.userQuestions({ questions: [question] }, { signal: new AbortController().signal }), { cancelled: true });
  }
});
