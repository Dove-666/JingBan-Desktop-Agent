import test from 'node:test';
import assert from 'node:assert/strict';
import { ActivityProjection } from '../activity.mjs';
import { COMPUTER_ACTIONS, computerSubject, operationSubject, publicCommentary, safePublicName } from '../activity-subject.mjs';

function fixture() {
  const events = [];
  const projection = new ActivityProjection('activity-fixture', event => events.push(event));
  const send = (type, data, sessionId = 'session-root') => projection.notification({
    method: 'session.event', params: { sessionId, event: { type, data } },
  });
  const call = (name, args, id = 'call-1', session = 'session-root') => send('tool/call',
    { name, arguments: JSON.stringify(args), callId: id, turn: 1, step: 1 }, session);
  const result = (id, value, isError = false, session = 'session-root') => send('tool/result', {
    message: { toolCallId: id, isError, content: [{ type: 'text', text: JSON.stringify(value) }] },
  }, session);
  return { events, projection, send, call, result };
}

test('official file_path and web URL become only basename and hostname', () => {
  assert.deepEqual(operationSubject('read', JSON.stringify({ file_path: 'C:\\PRIVATE\\report.txt', token: 'PRIVATE_TOKEN' })),
    { kind: 'file', name: 'report.txt' });
  assert.deepEqual(operationSubject('write', { file_path: '/private/report.md', content: 'PRIVATE_CONTENT' }),
    { kind: 'file', name: 'report.md' });
  assert.deepEqual(operationSubject('web_fetch', { url: 'https://user:SECRET@example.com/private?q=SECRET#SECRET' }),
    { kind: 'website', name: 'example.com' });
  assert.equal(operationSubject('web_search', { query: 'PRIVATE_SEARCH' }), null);
  assert.equal(operationSubject('read', '{not json PRIVATE'), null);
  assert.equal(operationSubject('read', { file_path: 'C:\\PRIVATE\\bad\nname.txt' }), null);
  assert.equal(operationSubject('web_fetch', { url: 'data:PRIVATE' }), null);
});

test('multi-file delivery, patches, commands and delegations expose bounded subjects', () => {
  assert.deepEqual(operationSubject('present', { files: [{ path: '/secret/a.pdf' }, { path: 'C:\\secret\\b.pdf' }] }), { kind: 'files', count: 2 });
  assert.deepEqual(operationSubject('apply_patch', { input: '*** Begin Patch\n*** Update File: /private/a.txt\n@@\n-secret contents\n+secret replacements\n*** End Patch' }),
    { kind: 'file', name: 'a.txt' });
  assert.deepEqual(operationSubject('pwsh', { description: '运行项目测试', command: 'PRIVATE_COMMAND' }),
    { kind: 'command', name: '运行项目测试' });
  assert.deepEqual(operationSubject('subagent', { name: 'review', description: 'Review task', prompt: 'PRIVATE_PROMPT' }),
    { kind: 'agent', action: 'start', name: 'review' });
  assert.equal(operationSubject('pwsh', { description: 'read C:\\private', command: 'PRIVATE_COMMAND' }), null);
  assert.equal(operationSubject('pwsh', { description: 'api_key=PRIVATE', command: 'PRIVATE_COMMAND' }), null);
  assert.deepEqual(operationSubject('todo_write', { todos: [{ content: 'PRIVATE_PLAN' }] }), { kind: 'plan', count: 1 });
  for (const value of ['https://private.example', 'C:\\secret', '../private', 'sk-0123456789abcdefgh', 'a'.repeat(97), 'bad\nname', 'x; Remove-Item', 'x|y']) {
    assert.equal(safePublicName(value), null, value);
  }
});

test('raw CUA method becomes a concrete action without input, coordinates or handles', () => {
  const methods = ['getCapabilities', 'listApps', 'findApps', 'getApp', 'activate', 'show', 'listWindows', 'getState',
    'listActions', 'performAction', 'invoke', 'mouseMove', 'click', 'setValue', 'selectText', 'typeText', 'paste',
    'pressKey', 'sendKeyEvent', 'scroll', 'drag', 'screenshot'];
  for (const method of methods) {
    const subject = computerSubject({ method, params: { text: 'PRIVATE_INPUT', value: 'PRIVATE_VALUE', query: 'PRIVATE_QUERY',
      x: 123, y: 456, appHandle: 'PRIVATE_HANDLE', key: 'PRIVATE_KEY' } }, '记事本');
    assert.ok(COMPUTER_ACTIONS.includes(subject.action));
    assert.deepEqual(Object.keys(subject).sort(), ['action', 'kind', 'name']);
    assert.ok(!JSON.stringify(subject).includes('PRIVATE'));
  }
  assert.equal(computerSubject({ method: 'futureArbitraryCode', params: {} }), null);
  assert.equal(computerSubject({ method: 'click', params: { clickCount: 2 } }).action, 'double_click');
  assert.equal(computerSubject({ method: 'click', params: { mouseButton: 'right' } }).action, 'right_click');
  assert.equal(computerSubject({ method: 'scroll', params: { deltaLinesY: -2 } }).action, 'scroll_up');
  assert.equal(computerSubject({ method: 'scroll', params: { deltaLinesX: 2, deltaLinesY: 0 } }).action, 'scroll_right');
});

test('selected app identity stays session-scoped and failed selection cannot name later operations', () => {
  const { events, projection, call, result } = fixture();
  call('cua', { method: 'getApp', params: { selector: 'C:\\PRIVATE\\notepad.exe' } });
  assert.deepEqual(events.at(-1).subject, { kind: 'computer', action: 'open_app', name: 'notepad.exe' });
  result('call-1', { appHandle: 'PRIVATE_HANDLE', name: '记事本', running: true });
  assert.deepEqual(events.at(-1).subject, { kind: 'computer', action: 'open_app', name: '记事本' });
  call('cua', { method: 'typeText', params: { appHandle: 'PRIVATE_HANDLE', text: 'PRIVATE_INPUT' } }, 'call-2');
  assert.deepEqual(events.at(-1).subject, { kind: 'computer', action: 'input', name: '记事本' });
  result('call-2', { secret: 'PRIVATE_RESULT' });
  call('cua', { method: 'screenshot', params: { appHandle: 'PRIVATE_HANDLE' } }, 'call-3', 'session-child');
  assert.deepEqual(events.at(-1).subject, { kind: 'computer', action: 'screenshot' });
  result('call-3', { data: 'PRIVATE_IMAGE' }, false, 'session-child');
  call('cua', { method: 'getApp', params: { selector: 'missing.exe' } }, 'call-4');
  result('call-4', { appHandle: 'FAILED_HANDLE', name: 'not selected', running: true }, true);
  call('cua', { method: 'click', params: { appHandle: 'FAILED_HANDLE' } }, 'call-5');
  assert.deepEqual(events.at(-1).subject, { kind: 'computer', action: 'click' });
  result('call-5', {});
  assert.equal(projection.calls.size, 0);
  assert.ok(!JSON.stringify(events).includes('PRIVATE'));
});

test('official child lifecycle never treats successful dispatch or an aborted run as goal completion', () => {
  const { events, projection, call, result } = fixture();
  call('subagent', { name: 'review', prompt: 'PRIVATE_PROMPT' });
  result('call-1', { kind: 'activation', name: 'review' });
  assert.equal(events.at(-1).subject.action, 'start');
  projection.notification({ method: 'subagent.started', params: { parentSessionId: 'session-root', childSessionId: 'session-child' } });
  const started = events.at(-1);
  assert.equal(started.status, 'running');
  projection.notification({ method: 'subagent.finished', params: { parentSessionId: 'session-root', childSessionId: 'session-child',
    status: 'ok', stopReason: 'aborted', lastAssistantMessage: [{ type: 'reasoning', text: 'PRIVATE' }] } });
  assert.equal(events.at(-1).event_id, started.event_id);
  assert.equal(events.at(-1).subject.action, 'stop');
  assert.equal(events.at(-1).status, 'failed');
  projection.notification({ method: 'subagent.finished', params: { parentSessionId: 'session-root', childSessionId: 'another-child',
    status: 'ok', stopReason: 'completed' } });
  assert.equal(events.at(-1).subject.action, 'finish');
  projection.notification({ method: 'subagent.finished', params: { parentSessionId: 'session-root', childSessionId: 'failed-child',
    status: 'ok', stopReason: 'refusal' } });
  assert.equal(events.at(-1).subject.action, 'fail');
  assert.ok(!JSON.stringify(events).includes('PRIVATE'));
});

test('only settled visible assistant text before a matching tool dispatch becomes commentary', () => {
  const { events, send, call } = fixture();
  const message = { role: 'assistant', content: [{ type: 'text', text: '先查看项目文件，再运行测试。' },
    { type: 'reasoning', text: 'PRIVATE_THOUGHTS' }, { type: 'tool-call', id: 'call-1', name: 'read', arguments: 'PRIVATE_ARGS' },
    { type: 'tool-call', id: 'call-2', name: 'read', arguments: 'PRIVATE_ARGS' }] };
  send('assistant/message', { turn: 1, step: 1, message, stream: ['PRIVATE_STREAM'] });
  assert.equal(events.length, 0);
  call('read', { file_path: '/private/README.md' });
  assert.equal(events[0].kind, 'commentary');
  assert.equal(events[0].text, '先查看项目文件，再运行测试。');
  call('read', { file_path: '/private/package.json' }, 'call-2');
  assert.equal(events.filter(event => event.kind === 'commentary').length, 1);
  send('assistant/message', { turn: 1, step: 2, message: { role: 'assistant', content: [{ type: 'text', text: 'FINAL_RESPONSE' }] } });
  send('turn/end', { turn: 1 });
  call('read', { file_path: '/private/next.txt' }, 'call-3');
  send('assistant/message', { turn: 1, step: 1, interrupted: true, message });
  call('read', { file_path: '/private/next.txt' });
  assert.equal(events.filter(event => event.kind === 'commentary').length, 1);
  assert.ok(!JSON.stringify(events).includes('PRIVATE_'));
  assert.ok(!JSON.stringify(events).includes('FINAL_RESPONSE'));
});

test('commentary cannot cross step/session boundaries and redacts code, credentials and private locations', () => {
  const { events, send, call } = fixture();
  const message = { role: 'assistant', content: [{ type: 'text', text: 'not yet dispatched' },
    { type: 'tool-call', id: 'call-1', name: 'read', arguments: '{}' }] };
  send('assistant/message', { turn: 1, step: 1, message });
  call('read', { file_path: 'child.txt' }, 'call-1', 'session-child');
  send('step/start', { turn: 1, step: 2 });
  call('read', { file_path: 'root.txt' });
  assert.equal(events.some(event => event.kind === 'commentary'), false);
  const cleaned = publicCommentary('说明。<think>PRIVATE_THOUGHT</think>\n```sh\nPRIVATE_CODE\n```\n`PRIVATE_INLINE`\n'
    + '访问 https://user:PRIVATE_PASS@example.com/private?key=PRIVATE_QUERY。\n'
    + '读取 C:\\PRIVATE_USER\\data.txt。\n读取 /private/PRIVATE_DIR/data.txt\n'
    + 'Bearer PRIVATE_TOKEN\napi_key=PRIVATE_API\nsk-0123456789abcdefghijklmnop\ndata:image/png;base64,PRIVATE_PIXELS');
  assert.ok(cleaned.includes('说明。'));
  assert.ok(cleaned.includes('example.com'));
  assert.ok(!cleaned.includes('PRIVATE'));
  assert.ok(!cleaned.includes('0123456789'));
  assert.equal(publicCommentary('<reasoning>never closed'), '');
  assert.equal(publicCommentary('a'.repeat(4000)).length, 3000);
});

test('quoted credential values are completely removed before host rendering', () => {
  for (const text of ['password: "PRIVATE_ALPHA PRIVATE_BETA" then continue',
    "password: 'PRIVATE_ALPHA PRIVATE_BETA' then continue", '"password": "PRIVATE_ALPHA PRIVATE_BETA"',
    'password: "PRIVATE_ALPHA PRIVATE_BETA', 'password: "PRIVATE_ALPHA PRIVATE_BETA\nnext line']) {
    assert.ok(!publicCommentary(text).includes('PRIVATE_'));
  }
  assert.equal(publicCommentary('Checking \u202efile'), 'Checking file');
  assert.equal(safePublicName('a\u202etxt'), null);
});
