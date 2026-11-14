/** Opt-in real Windows regression. Only the separately launched owned fixture is controlled. */
import assert from 'node:assert/strict';
import test from 'node:test';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

test('SDK cancels an active native input and acknowledges release', {
  timeout: 180000, skip: process.platform !== 'win32' || !process.env.JINGBAN_DSH_NATIVE_FIXTURE,
}, async t => {
  const fixturePath = resolve(process.env.JINGBAN_DSH_NATIVE_FIXTURE);
  const holdMs = Number(process.env.JINGBAN_DSH_NATIVE_HOLD_MS ?? 3500);
  assert.ok(Number.isInteger(holdMs) && holdMs >= 1000 && holdMs <= 5000);
  const preClickFocus = process.env.JINGBAN_DSH_NATIVE_LEGACY_FOCUS !== '1';
  const backend = process.env.JINGBAN_DSH_NATIVE_BACKEND;
  const { HarnessRunner } = await import(backend ? pathToFileURL(join(resolve(backend), 'runner.mjs')).href : '../runner.mjs');
  const directory = await mkdtemp(join(tmpdir(), '鲸伴 原生停止 '));
  const fixture = spawn(fixturePath, [], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const exited = once(fixture, 'exit');
  const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]();
  const next = async () => { const line = await lines.next(); assert.equal(line.done, false); return JSON.parse(line.value); };
  const command = async value => { fixture.stdin.write(value + '\n'); return next(); };
  const initial = await next();
  assert.ok(Number.isInteger(initial.pid));
  const events = [], errors = [], sockets = new Set();
  let count = 0, runner, appHandle, target, checks = [], passed = false, failure, nativeDiagnostic;
  let signalPressed, rejectPressed;
  const pressed = new Promise((yes, no) => { signalPressed = yes; rejectPressed = no; });
  pressed.catch(() => {});
  const deadline = setTimeout(() => rejectPressed(new Error('OWNED_INPUT_NOT_OBSERVED')), 120000);
  const textOf = message => typeof message.content === 'string' ? message.content :
    message.content.filter(item => item.type === 'text').map(item => item.text).join('\n');
  const call = (method, params = {}) => ({ tool: 'cua', arguments: { method, params } });
  const element = (state, name) => {
    const row = state.tree.split('\n').find(row => row.includes(name));
    assert.ok(row, `Missing owned control: ${name}`);
    return { ...target, snapshotId: state.snapshotId, elementId: row.match(/\[([^\]]+)\]/)[1] };
  };
  const server = http.createServer(async (req, res) => {
    try {
      let raw = ''; for await (const chunk of req) raw += chunk;
      assert.equal(req.headers.authorization, 'Bearer owned-native-test');
      const body = JSON.parse(raw), turn = ++count;
      let answer;
      if (turn === 1) answer = call('getApp', { selector: fixturePath });
      else if (turn === 2) {
        const app = JSON.parse(textOf(body.messages.at(-1))); appHandle = app.appHandle;
        assert.ok(appHandle, 'Native app selection failed');
        answer = call('listWindows', { appHandle });
      } else if (turn === 3) {
        const windows = JSON.parse(textOf(body.messages.at(-1)));
        assert.ok(Array.isArray(windows), 'Native window schema failed');
        const window = windows.find(item => item.title === 'DSH CUA owned test window');
        assert.ok(window, 'Only the exact owned fixture window may be used');
        target = { appHandle, windowId: window.windowId };
        answer = call('getState', { ...target, details: true });
      } else if (turn === 4) {
        const state = JSON.parse(textOf(body.messages.at(-1)));
        answer = call('setValue', { ...element(state, 'CUA test text'), value: '鲸伴 SDK 输入验证 😀' });
      } else if (turn === 5) {
        assert.equal((await command('inspect')).text, '鲸伴 SDK 输入验证 😀');
        checks.push('sdk-window-state-uia-input');
        answer = call('screenshot', target);
      } else if (turn === 6) {
        const blocks = body.messages.flatMap(item => Array.isArray(item.content) ? item.content : []);
        assert.ok(blocks.some(item => item.type === 'image_url'), 'Native screenshot must reach the model');
        checks.push('sdk-owned-screenshot-to-model');
        const focused = await command('focus');
        assert.equal(focused.foreground, initial.state.window);
        answer = call('getState', { ...target, details: true });
      } else if (turn === 7) {
        const state = JSON.parse(textOf(body.messages.at(-1)));
        if (preClickFocus) {
          const focused = await command('focus');
          assert.equal(focused.foreground, initial.state.window);
        }
        answer = call('click', { ...element(state, 'Record click'), holdMs });
        void (async () => {
          for (let attempt = 0; attempt < 600; attempt++) {
            const value = await command('inspect');
            if (value.buttons === 'Left') { signalPressed(value); return; }
            await new Promise(resolve => setTimeout(resolve, 10));
          }
          throw new Error('Native button never became pressed');
        })().catch(rejectPressed);
      } else {
        nativeDiagnostic = textOf(body.messages.at(-1)).slice(0, 2000);
        throw new Error('Native input finished before cancellation was observed');
      }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const frame = delta => ({ id: 'fixture', object: 'chat.completion.chunk', created: 1,
        model: 'fixture', choices: [{ index: 0, delta, finish_reason: null }] });
      res.write(`data: ${JSON.stringify(frame({ role: 'assistant' }))}\n\n`);
      res.write(`data: ${JSON.stringify(frame({ tool_calls: [{ index: 0, id: `call-${turn}`, type: 'function',
        function: { name: answer.tool, arguments: JSON.stringify(answer.arguments) } }] }))}\n\n`);
      const end = frame({}); end.choices[0].finish_reason = 'tool_calls';
      res.end(`data: ${JSON.stringify(end)}\n\ndata: [DONE]\n\n`);
    } catch (error) {
      errors.push(error); rejectPressed(error);
      if (!res.headersSent) res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'Owned native fixture failed.' } }));
    }
  });
  server.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    clearTimeout(deadline);
    await runner?.cancel().catch(() => {});
    for (const socket of sockets) socket.destroy(); server.close();
    fixture.stdin.end('close\n');
    const timer = setTimeout(() => fixture.kill(), 3000);
    await exited; clearTimeout(timer);
    if (process.env.JINGBAN_DSH_NATIVE_REPORT) await writeFile(process.env.JINGBAN_DSH_NATIVE_REPORT,
      JSON.stringify({ passed, checks, model_requests: count, real_api_calls: 0, hold_ms: holdMs, pre_click_focus: preClickFocus,
        boundary: 'official SDK + native Windows helper + exclusively owned fixture',
        foreground_setup: 'The owned test fixture explicitly prepares its own foreground before pointer input. This does not verify SDK foreground activation.',
        ...(failure ? { failure, native_tool_diagnostic: nativeDiagnostic } : {}) }, null, 2) + '\n');
  });
  try {
    runner = new HarnessRunner({ emit: event => {
      events.push(event);
      if (event.type === 'interaction') {
        // Only consent to the exact disposable executable selected above.
        assert.ok(event.question.includes('dsh-cua-desktop-fixture'));
        assert.ok(event.options.includes('Allow for this session'));
        void runner.control({ type: 'interaction_reply', interaction_id: event.interaction_id,
          answer: { text: 'Allow for this session' } });
      }
    } });
    const running = runner.run({ type: 'run', id: 'owned-native-stop', task: 'Test only the disposable owned fixture.',
      config: { base_url: `http://127.0.0.1:${server.address().port}/v1`, model: 'fixture',
        api_key: 'owned-native-test', supports_vision: true },
      options: { workspace: join(directory, 'workspace'), tools_enabled: false, computer_enabled: true },
      storage: { home: join(directory, 'home'), session_id: null }, context: {},
    }).then(() => ({ completed: true }), error => ({ error }));
    const held = await pressed;
    clearTimeout(deadline);
    assert.equal(held.foreground, initial.state.window);
    const started = Date.now();
    await runner.cancel();
    assert.ok((await running).error, 'A cancelled task must not report completion');
    assert.ok(events.some(event => event.type === 'stopped' && event.computer_settled === true),
      'The host needs positive native cleanup acknowledgement');
    assert.ok(!events.some(event => event.type === 'final'));
    assert.equal(errors.length, 0);
    const stopped = await command('inspect');
    assert.equal(stopped.buttons, 'None');
    await new Promise(resolve => setTimeout(resolve, 1200));
    const stable = await command('inspect');
    assert.equal(stable.buttons, 'None');
    assert.equal(stable.clicks, stopped.clicks);
    assert.equal(stable.text, stopped.text);
    assert.ok(Date.now() - started < 30000);
    checks.push('native-input-observed-before-stop', 'native-release-acknowledged', 'no-input-after-stop');
    passed = true;
  } catch (error) {
    failure = { code: error.code ?? error.name, message: error.message };
    throw error;
  }
});
