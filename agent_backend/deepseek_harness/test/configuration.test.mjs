import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { launchConfiguration, promptBlocks, reasoningOptions, runtimeEnvironment } from '../configuration.mjs';
import { ActivityProjection } from '../activity.mjs';

const frame = () => ({ type: 'run', id: 'run-1', task: '新消息',
  config: { base_url: 'https://example.test/v1', model: 'model', api_key: 'synthetic-key' },
  options: { workspace: resolve('workspace'), tools_enabled: true, computer_enabled: false },
  storage: { home: resolve('runtime'), session_id: null }, context: {} });

test('only the explicitly attached current image becomes an SDK image block', () => {
  const input = frame();
  input.context.messages = [{ role: 'system', content: 'obsolete JSON response instructions' },
    { role: 'user', content: [{ type: 'text', text: 'old' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,QQ==' } }] },
    { role: 'assistant', content: 'previous' }, { role: 'user', content: input.task }];
  const blocks = promptBlocks(input);
  assert.equal(blocks.filter(block => block.type === 'image').length, 0);
  assert.equal(blocks.filter(block => block.text === input.task).length, 1);
  assert.ok(!JSON.stringify(blocks).includes('obsolete JSON'));
  assert.ok(!JSON.stringify(blocks).includes('image_url'));
  input.context.current_image_url = 'data:image/png;base64,QQ==';
  assert.deepEqual(promptBlocks(input).at(-1), { type: 'image', mimeType: 'image/png', data: 'QQ==' });
  input.storage.session_id = 'session-saved';
  assert.equal(promptBlocks(input).length, 2);
});

test('a launch patch has no key and disabled task tools are actually removed', () => {
  const input = frame();
  input.options.tools_enabled = false;
  input.context.personality = '说话简洁，喜欢鲸鱼。';
  const { patches } = launchConfiguration(input);
  assert.ok(!JSON.stringify(patches).includes(input.config.api_key));
  for (const id of ['tool-fs', 'tool-fs-search', 'tool-bash', 'tool-pwsh', 'tool-subagent', 'tool-workflow', 'tool-office']) {
    assert.equal(patches.find(row => row.id === id)?.disabled, true);
  }
  assert.ok(JSON.stringify(patches).includes(input.context.personality));
  assert.ok(!JSON.stringify(patches).includes('cua-windows'));
});

test('only the selected API key enters the child environment', () => {
  const env = runtimeEnvironment(frame().config, { PATH: 'bin', TEMP: 'temp', OPENAI_API_KEY: 'unrelated',
    ANTHROPIC_AUTH_TOKEN: 'other', DSH_HOME: 'other-user-state', NODE_OPTIONS: '--import injected.mjs' });
  assert.equal(env.JINGBAN_DSH_API_KEY, 'synthetic-key');
  assert.equal(env.PATH, 'bin');
  for (const key of ['OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'DSH_HOME', 'NODE_OPTIONS']) assert.ok(!(key in env));
  assert.equal(env.DSH_TELEMETRY_DISABLED, '1');
});

test('model-declared effort values are mapped without inventing a provider level', () => {
  const deepseek = reasoningOptions({ profile: 'deepseek', value: 'max', values: ['default', 'none', 'low', 'high', 'max'] });
  assert.equal(deepseek.effort, 'max');
  assert.equal(deepseek.model.compat.thinkingFormat, 'deepseek');
  assert.equal(deepseek.model.reasoningEfforts.off, null);
  const disabled = reasoningOptions({ profile: 'deepseek', value: 'none', values: ['default', 'none', 'high'] });
  assert.equal(disabled.model.reasoningEfforts.off, 'none');
  assert.equal(disabled.effort, 'off');
  assert.deepEqual(reasoningOptions({ profile: 'auto', value: 'max' }), {});
  assert.equal(reasoningOptions({ profile: 'openai', value: 'max', values: ['default', 'low'] }).effort, undefined);
});

test('public activity excludes reasoning, command arguments and tool results', () => {
  const frames = [];
  const activity = new ActivityProjection('run-1', value => frames.push(value));
  const send = event => activity.notification({ method: 'session.event', params: { sessionId: 'session-1', event } });
  send({ type: 'assistant/message', data: { message: { content: [{ type: 'reasoning', text: 'PRIVATE' }] } } });
  send({ type: 'tool/call', data: { callId: 'call-1', name: 'pwsh', arguments: 'SECRET ARGUMENT' } });
  send({ type: 'tool/result', data: { message: { toolCallId: 'call-1', isError: true, content: [{ type: 'text', text: 'SECRET RESULT' }] } } });
  assert.equal(frames.length, 2);
  assert.equal(frames[1].status, 'failed');
  assert.equal(frames[0].event_id, frames[1].event_id);
  assert.ok(!JSON.stringify(frames).includes('SECRET'));
  assert.ok(!JSON.stringify(frames).includes('PRIVATE'));
});
