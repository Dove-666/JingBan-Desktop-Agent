import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { validateRun, promptBlocks } from '../configuration.mjs';
import { HarnessRunner } from '../runner.mjs';

const frame = task => ({ type: 'run', id: 'unicode', task,
  config: { base_url: 'https://example.test/v1', model: 'model', api_key: 'fixture-key' },
  options: { workspace: resolve('workspace') }, storage: { home: resolve('home') }, context: {} });

test('task and history boundaries count Unicode code points without cutting a surrogate pair', () => {
  const valid = frame('🐳'.repeat(12000));
  assert.equal(validateRun(valid), valid);
  assert.throws(() => validateRun(frame('🐳'.repeat(12001))), /INVALID_REQUEST/);
  valid.context.messages = [{ role: 'assistant', content: '🐳'.repeat(12001) }];
  const text = promptBlocks(valid)[0].text;
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(text));
  assert.equal(Array.from(text).filter(char => char === '🐳').length, 12000);
});

test('a valid supplementary instruction is retained and overflow is explicitly rejected', async () => {
  const runner = new HarnessRunner();
  const text = '🐳'.repeat(12000);
  await runner.control({ type: 'steer', text });
  assert.deepEqual(runner.steers, [text]);
  await assert.rejects(runner.control({ type: 'steer', text: `${text}🐳` }), { code: 'INVALID_REQUEST' });
  assert.deepEqual(runner.steers, [text]);
});

test('question and option limits share the host Unicode contract', async () => {
  const runner = new HarnessRunner();
  const label = '🐳'.repeat(240);
  let calls = 0;
  runner.ask = async question => { calls++; assert.equal(question.options[0], label); return { text: label }; };
  const question = { id: 'one', question: '🐳'.repeat(16000), options: [{ label }] };
  assert.deepEqual(await runner.userQuestions({ questions: [question] }, {}), {
    answers: [{ id: 'one', selected: [label] }],
  });
  assert.deepEqual(await runner.userQuestions({ questions: [{ ...question, question: `${question.question}🐳` }] }, {}), { cancelled: true });
  assert.deepEqual(await runner.userQuestions({ questions: [{ ...question, options: [{ label: `${label}🐳` }] }] }, {}), { cancelled: true });
  assert.equal(calls, 1);
});
