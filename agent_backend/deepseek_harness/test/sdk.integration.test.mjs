import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { deflateSync, crc32 } from 'node:zlib';
import { HarnessRunner } from '../runner.mjs';
import { inspectComputerRuntime } from '../computer.mjs';

const KEY = 'synthetic-dsh-integration-secret';

function testPng() {
  const chunk = (name, data) => {
    const type = Buffer.from(name);
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(Buffer.concat([type, data])));
    return Buffer.concat([length, type, data, crc]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(2, 0);
  header.writeUInt32BE(2, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.from([0,255,0,0,0,255,0,0,0,0,255,255,255,255]))), chunk('IEND', Buffer.alloc(0))]).toString('base64');
}

async function serverFixture(t, responder) {
  const requests = [];
  const errors = [];
  const sockets = new Set();
  const server = http.createServer(async (req, res) => {
    try {
      let raw = '';
      for await (const part of req) raw += part;
      const body = JSON.parse(raw);
      requests.push(body);
      assert.equal(req.headers.authorization, `Bearer ${KEY}`);
      assert.equal(req.url, '/v1/chat/completions');
      const result = await responder(body, requests.length);
      if (res.destroyed) return;
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const chunk = delta => ({ id: 'chatcmpl-jingban-fixture', object: 'chat.completion.chunk', created: 1,
        model: 'fixture-model', choices: [{ index: 0, delta, finish_reason: null }] });
      res.write(`data: ${JSON.stringify(chunk({ role: 'assistant' }))}\n\n`);
      if (result.tool) {
        res.write(`data: ${JSON.stringify(chunk({ tool_calls: [{ index: 0, id: `call-${requests.length}`, type: 'function',
          function: { name: result.tool, arguments: JSON.stringify(result.arguments) } }] }))}\n\n`);
      } else res.write(`data: ${JSON.stringify(chunk({ content: result.text ?? 'done' }))}\n\n`);
      const end = chunk({});
      end.choices[0].finish_reason = result.tool ? 'tool_calls' : 'stop';
      res.end(`data: ${JSON.stringify(end)}\n\ndata: [DONE]\n\n`);
    } catch (error) {
      errors.push(error);
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: error.message } }));
    }
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { for (const socket of sockets) socket.destroy(); server.close(); });
  return { baseURL: `http://127.0.0.1:${server.address().port}/v1`, requests, errors };
}

async function runFixture(t, { responder, tools = true } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'jingban-dsh-sdk-'));
  const server = await serverFixture(t, responder);
  const events = [];
  const runner = new HarnessRunner({ emit: event => events.push(event) });
  if (process.env.DSH_TEST_DIAGNOSTICS) {
    const close = runner.performClose.bind(runner);
    runner.performClose = async () => {
      const client = runner.harness?.client;
      if (!client) return close();
      const request = client.request.bind(client);
      const shutdowns = [];
      client.request = async (...args) => {
        if (args[0] !== 'shutdown') return request(...args);
        try { const result = await request(...args); shutdowns.push({ acknowledged: true }); return result; }
        catch (error) {
          shutdowns.push({ acknowledged: false, name: error.name, code: error.code ?? null, message: error.message });
          throw error;
        }
      };
      await close();
      if (!runner.shutdownConfirmed) console.error(JSON.stringify({ shutdown_diagnostic: {
        shutdowns, exit_code: client.child?.exitCode, signal_code: client.child?.signalCode,
        killed: client.child?.killed, stderr: client.stderrTail,
      } }));
    };
  }
  t.after(() => runner.cancel());
  const frame = { type: 'run', id: 'fixture-task', task: 'Create a report and deliver it.',
    config: { base_url: server.baseURL, model: 'fixture-model', api_key: KEY, supports_vision: false },
    options: { workspace: join(directory, 'workspace'), tools_enabled: tools, computer_enabled: false },
    storage: { home: join(directory, 'home'), session_id: null }, context: {} };
  return { directory, server, events, runner, frame };
}

test('official SDK executes write and present, persists and resumes its own session', { timeout: 180000 }, async t => {
  const fixture = await runFixture(t, { responder: (body, count) => {
    const names = body.tools.map(tool => tool.function.name);
    assert.ok(names.includes('write'));
    assert.ok(names.includes('present'));
    if (count === 1) return { tool: 'write', arguments: { file_path: 'result.txt', content: 'DSH wrote this file.\n' } };
    if (count === 2) {
      assert.equal(body.messages.at(-1).role, 'tool');
      assert.ok(body.messages.at(-1).content.includes('result.txt'));
      return { tool: 'present', arguments: { files: [{ path: 'result.txt' }] } };
    }
    if (count === 3) return { text: '报告已保存。' };
    assert.ok(JSON.stringify(body.messages).includes('DSH wrote this file.'));
    assert.equal(body.messages.filter(message => message.content === 'Create a report and deliver it.').length, 1);
    return { text: '已记住刚才的报告。' };
  } });
  const result = await fixture.runner.run(fixture.frame);
  assert.equal(result.status, 'completed');
  assert.equal(result.reply, '报告已保存。');
  assert.equal(fixture.events.find(event => event.type === 'stopped')?.computer_settled, true);
  assert.ok(fixture.events.findIndex(event => event.type === 'stopped') < fixture.events.findIndex(event => event.type === 'final'));
  assert.equal(await readFile(join(fixture.frame.options.workspace, 'result.txt'), 'utf8'), 'DSH wrote this file.\n');
  assert.equal(result.artifacts.length, 1);
  assert.equal(result.artifacts[0].path, join(fixture.frame.options.workspace, 'result.txt'));
  assert.equal(result.artifacts[0].size_bytes, Buffer.byteLength('DSH wrote this file.\n'));
  assert.equal(result.artifacts[0].sha256, createHash('sha256').update('DSH wrote this file.\n').digest('hex'));
  assert.equal(fixture.events.filter(event => event.type === 'activity' && event.status === 'completed').length, 2);
  const second = new HarnessRunner({ emit: () => {} });
  t.after(() => second.cancel());
  const next = await second.run({ ...fixture.frame, id: 'fixture-second', task: 'Remember that report.',
    storage: { ...fixture.frame.storage, session_id: result.dsh_session_id } });
  assert.equal(next.dsh_session_id, result.dsh_session_id);
  assert.equal(next.reply, '已记住刚才的报告。');
  assert.equal(fixture.server.requests.length, 4);
  assert.ok(!JSON.stringify(fixture.events).includes(KEY));
  assert.ok(!(await readFile(join(fixture.frame.storage.home, 'jingban.launch.patch.json'), 'utf8')).includes(KEY));
  assert.ok((await readdir(join(fixture.frame.storage.home, 'sessions'))).length > 0);
});

test('the real SDK tool schemas obey the host task and computer permission switches', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { tools: false, responder: body => {
    const names = body.tools.map(tool => tool.function.name);
    for (const forbidden of ['read', 'write', 'edit', 'pwsh', 'bash', 'web_fetch', 'subagent', 'cua', 'cua_run_code']) {
      assert.ok(!names.includes(forbidden), `${forbidden} must be absent`);
    }
    return { text: '权限已按设置应用。' };
  } });
  const result = await fixture.runner.run(fixture.frame);
  assert.equal(result.reply, '权限已按设置应用。');
  assert.equal(result.status, 'completed');
});

test('official SDK questions return the real user answer and do not auto-approve', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { responder: (body, count) => {
    if (count === 1) return { tool: 'ask_user_question', arguments: { questions: [
      { id: 'choice', question: '选择报告颜色', options: [{ label: '蓝色' }, { label: '绿色' }] },
    ] } };
    assert.match(body.messages.at(-1).content, /绿色/);
    return { text: '使用绿色。' };
  } });
  fixture.runner.emit = event => {
    fixture.events.push(event);
    if (event.type === 'interaction') {
      assert.deepEqual(event.options, ['蓝色', '绿色']);
      fixture.runner.control({ type: 'interaction_reply', interaction_id: event.interaction_id,
        answer: { text: '绿色' } });
    }
  };
  const result = await fixture.runner.run(fixture.frame);
  assert.equal(result.reply, '使用绿色。');
  assert.equal(fixture.events.filter(event => event.type === 'interaction').length, 1);
});

test('a user correction reaches the ongoing native tool loop', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { responder: (body, count) => {
    if (count === 1) return { tool: 'write', arguments: { file_path: 'draft.txt', content: 'draft' } };
    // A request already dispatched before the UI sees tool/call is allowed to
    // finish; the native inbox must then consume the correction before idle.
    return { text: JSON.stringify(body.messages).includes('请用绿色') ? '已采用补充要求。' : '已保存草稿。' };
  } });
  let steered = false;
  fixture.runner.emit = event => {
    fixture.events.push(event);
    if (!steered && event.type === 'activity' && event.status === 'running') {
      steered = true;
      fixture.runner.control({ type: 'steer', text: '请用绿色' });
    }
  };
  assert.equal((await fixture.runner.run(fixture.frame)).reply, '已采用补充要求。');
  assert.ok(steered);
  assert.ok(fixture.server.requests.some(request => JSON.stringify(request.messages).includes('请用绿色')));
});

test('cancel closes the actual runtime while a provider request is pending', { timeout: 120000 }, async t => {
  let announce;
  const requestArrived = new Promise(resolve => { announce = resolve; });
  const fixture = await runFixture(t, { responder: () => { announce(); return new Promise(() => {}); } });
  const running = fixture.runner.run(fixture.frame);
  const rejected = assert.rejects(running);
  await requestArrived;
  const started = Date.now();
  await fixture.runner.cancel();
  await rejected;
  assert.ok(Date.now() - started < 25000);
  assert.ok(!fixture.events.some(event => event.type === 'final'));
  assert.equal(fixture.server.requests.length, 1);
  assert.equal(fixture.events.find(event => event.type === 'stopped')?.computer_settled, true);
});

test('the native shell does not inherit the provider credential', { timeout: 120000, skip: process.platform !== 'win32' }, async t => {
  const fixture = await runFixture(t, { responder: (body, count) => {
    if (count === 1) return { tool: 'pwsh', arguments: {
      command: "if ($env:JINGBAN_DSH_API_KEY) { Write-Output 'KEY_LEAKED' } else { Write-Output 'KEY_ABSENT' }",
      description: 'Verify provider credentials are absent from tools.',
    } };
    assert.match(body.messages.at(-1).content, /KEY_ABSENT/);
    assert.ok(!body.messages.at(-1).content.includes('KEY_LEAKED'));
    return { text: '凭据未传入工具进程。' };
  } });
  assert.equal((await fixture.runner.run(fixture.frame)).reply, '凭据未传入工具进程。');
});

test('SDK images are stored once and the next text turn adds no new attachment', { timeout: 180000 }, async t => {
  const fixture = await runFixture(t, { responder: (body, count) => {
    const images = body.messages.flatMap(message => Array.isArray(message.content) ? message.content : [])
      .filter(block => block.type === 'image_url');
    assert.equal(images.length, 1);
    if (count === 2) {
      const lastUser = body.messages.filter(message => message.role === 'user').at(-1);
      assert.ok(!JSON.stringify(lastUser).includes('image_url'));
    }
    assert.match(JSON.stringify(body.messages), /喜欢鲸鱼/);
    return { text: count === 1 ? '已收到本次图片。' : '本次只收到文字。' };
  } });
  fixture.frame.config.supports_vision = true;
  fixture.frame.context.personality = '你喜欢鲸鱼。';
  fixture.frame.context.current_image_url = `data:image/png;base64,${testPng()}`;
  const first = await fixture.runner.run(fixture.frame);
  const second = new HarnessRunner();
  t.after(() => second.cancel());
  const result = await second.run({ ...fixture.frame, id: 'image-second', task: '这次没有加图片。',
    context: { personality: fixture.frame.context.personality },
    storage: { ...fixture.frame.storage, session_id: first.dsh_session_id } });
  assert.equal(result.reply, '本次只收到文字。');
  assert.equal(fixture.server.requests.length, 2);
});

test('missing native session never silently creates a replacement or sends a model call', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { responder: () => ({ text: 'should not be called' }) });
  fixture.frame.storage.session_id = 'session-00000000000000000000000000000001';
  await assert.rejects(fixture.runner.run(fixture.frame), { code: 'DSH_SESSION_UNAVAILABLE' });
  assert.equal(fixture.server.requests.length, 0);
  assert.ok(!fixture.events.some(event => event.type === 'final'));
});

test('the shared DeepSeek effort reaches the actual adapter request', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { responder: body => {
    assert.deepEqual(body.thinking, { type: 'enabled' });
    assert.equal(body.reasoning_effort, 'max');
    assert.equal(body.messages[0].role, 'system');
    assert.ok(!body.messages.some(message => message.role === 'developer'));
    assert.equal(body.max_tokens, 32768);
    assert.equal(body.max_completion_tokens, undefined);
    assert.equal(body.store, undefined);
    return { text: '使用配置的思考强度。' };
  } });
  fixture.frame.config.reasoning = { profile: 'deepseek', value: 'max', control: 'effort',
    values: ['default', 'none', 'low', 'high', 'max'] };
  assert.equal((await fixture.runner.run(fixture.frame)).reply, '使用配置的思考强度。');
});

test('explicit thinking off reaches the adapter as disabled', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { responder: body => {
    assert.deepEqual(body.thinking, { type: 'disabled' });
    assert.equal(body.reasoning_effort, undefined);
    return { text: '已关闭本次思考。' };
  } });
  fixture.frame.config.reasoning = { profile: 'deepseek', value: 'none', control: 'effort',
    values: ['default', 'none', 'high', 'max'] };
  assert.equal((await fixture.runner.run(fixture.frame)).reply, '已关闭本次思考。');
});

test('a thinking toggle sends no unsupported effort parameter', { timeout: 120000 }, async t => {
  const fixture = await runFixture(t, { responder: body => {
    assert.deepEqual(body.thinking, { type: 'enabled' });
    assert.equal(body.reasoning_effort, undefined);
    return { text: '使用模型的思考开关。' };
  } });
  fixture.frame.config.reasoning = { profile: 'thinking', value: 'enabled', control: 'toggle',
    values: ['default', 'none', 'enabled'] };
  assert.equal((await fixture.runner.run(fixture.frame)).reply, '使用模型的思考开关。');
});

test('the official Windows computer tool can enumerate applications through the real SDK', {
  timeout: 120000, skip: !inspectComputerRuntime().available,
}, async t => {
  const fixture = await runFixture(t, { responder: (body, count) => {
    if (count === 1) {
      assert.ok(body.tools.some(tool => tool.function.name === 'cua'));
      assert.match(JSON.stringify(body.messages), /Windows: accessibility tree operations/);
      return { tool: 'cua', arguments: { method: 'listApps', params: {} } };
    }
    const toolMessage = body.messages.at(-1);
    assert.equal(toolMessage.role, 'tool');
    const apps = JSON.parse(toolMessage.content);
    assert.ok(Array.isArray(apps));
    // Raw cua returns native rows; the optional JS facade maps these to id/displayName.
    assert.ok(apps.every(app => Number.isInteger(app.pid) && typeof app.name === 'string'));
    return { text: '电脑工具已完成只读检查。' };
  } });
  fixture.frame.options.computer_enabled = true;
  let result;
  try { result = await fixture.runner.run(fixture.frame); }
  catch (error) {
    if (process.env.DSH_TEST_DIAGNOSTICS) console.error(fixture.runner.harness.client.stderrTail.join('\n'));
    throw fixture.server.errors[0] ?? error;
  }
  assert.equal(result.reply, '电脑工具已完成只读检查。');
  assert.equal(fixture.server.requests.length, 2);
  assert.ok(fixture.events.some(event => event.kind === 'computer' && event.status === 'completed'));
});

test('native present delivers an approved exact file outside the task workspace', { timeout: 120000 }, async t => {
  let externalPath;
  const fixture = await runFixture(t, { responder: (_body, count) => count === 1
    ? { tool: 'present', arguments: { files: [{ path: externalPath }] } }
    : { text: '外部文件已交付。' } });
  externalPath = join(fixture.directory, 'outside-report.txt');
  const bytes = Buffer.from('Explicit native delivery outside the task workspace.\n');
  await writeFile(externalPath, bytes);
  fixture.runner.emit = event => {
    fixture.events.push(event);
    if (event.type === 'interaction' && event.options.includes('允许本次')) {
      fixture.runner.control({ type: 'interaction_reply', interaction_id: event.interaction_id,
        answer: { text: '允许本次' } });
    }
  };
  const result = await fixture.runner.run(fixture.frame);
  assert.deepEqual(result.artifacts, [{ path: externalPath, name: 'outside-report.txt',
    size_bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }]);
  assert.ok(fixture.events.some(event => event.type === 'activity' && event.name === 'present' && event.status === 'completed'));
});

test('an enabled but unused computer provider confirms zero native channels after disposal', {
  timeout: 120000, skip: !inspectComputerRuntime().available,
}, async t => {
  const fixture = await runFixture(t, { responder: () => ({ text: '这次不需要操作电脑。' }) });
  fixture.frame.options.computer_enabled = true;
  assert.equal((await fixture.runner.run(fixture.frame)).reply, '这次不需要操作电脑。');
  const receipt = JSON.parse(await readFile(fixture.runner.nativeReceipt.path, 'utf8'));
  assert.equal(receipt.channels_started, 0);
  assert.equal(receipt.channels_closed, 0);
  assert.equal(fixture.runner.shutdownConfirmed, true);
  assert.equal(fixture.events.find(event => event.type === 'stopped')?.computer_settled, true);
});

test('SDK initialization failure retains cleanup proof from the disposed original client', {
  timeout: 120000, skip: !inspectComputerRuntime().available,
}, async t => {
  const { DeepSeekHarness } = await import('@deepseek-ai/dsh-sdk-client');
  const fixture = await runFixture(t, { responder: () => { throw new Error('No model request is allowed.'); } });
  fixture.frame.options.computer_enabled = true;
  fixture.runner.Harness = class extends DeepSeekHarness {
    constructor(options) {
      super(options);
      const initialize = this.client.initialize.bind(this.client);
      this.client.initialize = async (...args) => {
        await initialize(...args);
        throw new Error('fixture-handshake-rejected');
      };
    }
  };
  await assert.rejects(fixture.runner.run(fixture.frame), /fixture-handshake-rejected/);
  const original = fixture.runner.runtimeObservation;
  assert.notEqual(original.client, fixture.runner.harness.client, 'Official SDK must replace the failed client.');
  assert.equal(original.acknowledged, true);
  assert.equal(original.client.child.exitCode, 0);
  assert.equal(fixture.runner.harness.client.child, undefined, 'The replacement client must never start.');
  assert.equal(fixture.server.requests.length, 0);
  assert.equal(fixture.runner.shutdownConfirmed, true);
  assert.equal(fixture.events.find(event => event.type === 'stopped')?.computer_settled, true);
});
