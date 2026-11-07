/** Actual public execution protocol with finite owned packets and contexts. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../frontend/chat-ui/src/bridge.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, strict: true,
}, reportDiagnostics: true });
assert.deepEqual(compiled.diagnostics, []);
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled.outputText)(require, module, module.exports);
const { ChatStreamProtocol } = module.exports;
const document = 'a'.repeat(32), foreign = 'b'.repeat(32), digest = 'c'.repeat(64), changedDigest = 'd'.repeat(64);
const started = '2026-11-06T10:00:00.000001+00:00';
const context = { model: 'owned', policy: 'model', history_messages: 39, estimated_input_tokens: 4000,
  model_context_tokens: 32000, output_tokens: 1000, source_counts: { history: 39, current: 1 },
  messages: Array.from({ length: 40 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user',
    content: (`OWNED_CONTEXT_${index} ` + '海风与鲸鱼的合成记录。'.repeat(25)).slice(0, 200) })) };
const trace = (extra = {}) => ({ version: 1, state: 'running', started_at: started, duration_ms: 100,
  events: [{ id: 'phase:starting', kind: 'progress', label: 'OWNED starting', status: 'running', elapsed_ms: 0 }], ...extra });
const cursor = (revision, sequence) => ({ version: 1, document, revision, sequence });
const full = (revision = 1, executionSequence = 1, textSequence = 100, extra = {}) => ({
  conversationId: 'current', draftDocument: document, conversations: [{ id: 'current', title: 'Owned' }],
  messages: [{ id: 'prior', role: 'assistant', content: 'Previous reply' }, { id: 'active', role: 'user', content: 'Owned task',
    metadata: { request_status: 'pending', image_id: 'e'.repeat(64), execution: trace({ context }), execution_context_digest: digest } }],
  busy: true, requestInCurrent: true, drafts: { current: 'Unsent draft' },
  streamingReply: { conversationId: 'current', requestId: 'request1', text: 'First text', state: 'streaming' },
  streamCursor: cursor(revision, textSequence), executionCursor: cursor(revision, executionSequence), ...extra,
});
const patch = (revision = 1, sequence = 2, extra = {}) => ({
  ...cursor(revision, sequence), conversationId: 'current', messageId: 'active', contextDigest: digest,
  execution: trace({ duration_ms: 200, events: [{ id: 'tool1', kind: 'tool', label: 'OWNED working', status: 'running', elapsed_ms: 150 }] }), ...extra,
});
const textPatch = (revision, sequence, text = 'Latest text') => ({ ...cursor(revision, sequence), conversationId: 'current',
  streamingReply: { conversationId: 'current', requestId: 'request1', text, state: 'streaming' } });
const ready = () => { const protocol = new ChatStreamProtocol(true, true); protocol.initial(full()); return protocol; };
const tests = [];
function test(name, action) { action(); tests.push(name); }

test('execution delta changes one row and retains the exact full context object', () => {
  const protocol = ready(), before = protocol.current;
  const result = protocol.execution(patch());
  assert.equal(result.changed, true);
  assert.notEqual(protocol.current.messages, before.messages);
  assert.equal(protocol.current.messages[0], before.messages[0]);
  assert.notEqual(protocol.current.messages[1], before.messages[1]);
  assert.equal(protocol.current.messages[1].metadata.execution.context, before.messages[1].metadata.execution.context);
  assert.deepEqual(protocol.current.messages[1].metadata.execution.context.messages.map(item => item.content), context.messages.map(item => item.content));
  for (const key of ['conversations', 'drafts', 'streamingReply']) assert.equal(protocol.current[key], before[key]);
  assert.equal(protocol.current.messages[1].metadata.image_id, 'e'.repeat(64));
});

test('invalid data cannot consume sequence or replace a context', () => {
  const protocol = ready(), before = protocol.current;
  for (const extra of [
    { messageId: 'unknown' }, { conversationId: 'other' }, { document: foreign },
    { contextDigest: changedDigest }, { execution: trace({ started_at: '2026-11-06T11:00:00Z' }) },
    { execution: trace({ duration_ms: 1 }) }, { execution: trace({ context }) },
  ]) assert.equal(protocol.execution(patch(1, 500, extra)).changed, false);
  assert.equal(protocol.current, before);
  assert.equal(protocol.execution(patch()).changed, true);
});

test('independent text and execution sequences preserve both interleaved results', () => {
  const protocol = ready();
  assert.equal(protocol.patch(textPatch(1, 200)).changed, true);
  assert.equal(protocol.execution(patch()).changed, true);
  assert.equal(protocol.patch(textPatch(1, 201, 'Final text')).changed, true);
  assert.equal(protocol.current.streamingReply.text, 'Final text');
  assert.equal(protocol.current.messages[1].metadata.execution.events[0].label, 'OWNED working');
  assert.equal(protocol.current.executionCursor.sequence, 2);
});

test('context change full barrier establishes a fresh digest and complete source text', () => {
  const protocol = ready(), nextContext = { ...context, messages: context.messages.map((item, index) => ({ ...item, content: item.content + index })) };
  const rows = full().messages;
  rows[1] = { ...rows[1], metadata: { ...rows[1].metadata, execution: trace({ context: nextContext }), execution_context_digest: changedDigest } };
  assert.equal(protocol.full(full(2, 2, 100, { messages: rows })).changed, true);
  assert.equal(protocol.execution(patch(1, 900)).changed, false);
  assert.equal(protocol.execution(patch(2, 3)).changed, false);
  assert.equal(protocol.execution(patch(2, 3, { contextDigest: changedDigest })).changed, true);
  assert.deepEqual(protocol.current.messages[1].metadata.execution.context.messages, nextContext.messages);
});

test('terminal and new-attempt full barriers reject late same-message progress', () => {
  const protocol = ready(), rows = full().messages;
  rows[1].metadata.execution = trace({ state: 'cancelled', context });
  protocol.full(full(2, 2, 100, { messages: rows, busy: false }));
  assert.equal(protocol.execution(patch(2, 999)).changed, false);
  const nextRows = full().messages;
  nextRows[1].metadata.execution = trace({ started_at: '2026-11-06T10:01:00Z', context });
  protocol.full(full(3, 3, 100, { messages: nextRows }));
  assert.equal(protocol.execution(patch(3, 1000)).changed, false);
  assert.equal(protocol.execution(patch(3, 4, { execution: trace({ started_at: '2026-11-06T10:01:00Z', duration_ms: 300 }) })).changed, true);
});

test('two future lanes use one recovery and one buffered value per lane', () => {
  const protocol = ready();
  const result = protocol.execution(patch(3, 10));
  assert.equal(result.requestSnapshot, 1);
  for (let sequence = 11; sequence <= 30; sequence++) {
    assert.equal(protocol.execution(patch(3, sequence)).requestSnapshot, undefined);
    assert.equal(protocol.patch(textPatch(3, sequence + 200)).requestSnapshot, undefined);
  }
  assert.equal(protocol.bufferedPatches, 2);
  assert.equal(protocol.recovery(1, full()).requestSnapshot, undefined);
  assert.equal(protocol.snapshotInFlight, false);
  assert.equal(protocol.execution(patch(3, 31)).requestSnapshot, undefined);
  protocol.full(full(3, 9, 199));
  assert.equal(protocol.current.executionCursor.sequence, 31);
  assert.equal(protocol.current.streamCursor.sequence, 230);
  assert.equal(protocol.bufferedPatches, 0);
});

test('early foreign execution displacement gets one document-owned recovery', () => {
  const protocol = new ChatStreamProtocol(true, true);
  protocol.execution(patch(100, 100, { document: foreign }));
  protocol.execution(patch(2, 2));
  const result = protocol.initial(full());
  assert.equal(result.requestSnapshot, 1);
  assert.equal(protocol.current.executionCursor.revision, 1);
  protocol.recovery(1, full(3, 3, 100));
  assert.equal(protocol.current.executionCursor.revision, 3);
  assert.equal(protocol.snapshotInFlight, false);
});

test('missing capability and disposal keep legacy full snapshots usable', () => {
  const protocol = new ChatStreamProtocol(true);
  protocol.initial(full());
  assert.equal(protocol.execution(patch()).changed, false);
  assert.equal(protocol.full(full(2, 2, 100)).changed, true);
  protocol.dispose();
  assert.equal(protocol.execution(patch(3, 3)).changed, false);
  assert.equal(protocol.full(full(3, 3, 101)).changed, false);
});

test('a negotiated full frame must have one shared full revision across both cursors', () => {
  const protocol = ready(), before = protocol.current;
  assert.equal(protocol.full(full(2, 2, 101, { executionCursor: cursor(3, 2) })).changed, false);
  assert.equal(protocol.current, before);
  assert.equal(protocol.full(full(2, 2, 101)).changed, true);
  assert.equal(protocol.execution(patch(2, 3)).changed, true);
  const fallback = new ChatStreamProtocol(true, true);
  fallback.initial(full(1, 1, 100, { executionCursor: cursor(9, 1) }));
  assert.equal(fallback.current.executionCursor, undefined);
  assert.equal(fallback.execution(patch()).changed, false);
  assert.equal(fallback.full(full(2, 2, 101)).changed, true);
  assert.equal(fallback.current.executionCursor, undefined);
});

test('actual hook subscribes execution before ready and shares bounded recovery through cleanup', () => {
  const slots = [], effects = [], requests = [], dispatches = [];
  let hookIndex = 0;
  function event() {
    const callbacks = new Set();
    return { callbacks, connect: callback => callbacks.add(callback), disconnect: callback => callbacks.delete(callback),
      send: value => [...callbacks].forEach(callback => callback(JSON.stringify(value))) };
  }
  const backend = { getState: callback => requests.push(callback), stateChanged: event(), streamingChanged: event(),
    executionChanged: event(), thumbnailReady: event(),
    dispatch: (action, payload, callback) => { dispatches.push({ action, payload: JSON.parse(payload) }); callback?.('{"ok":true}'); } };
  const react = { useState: initial => {
    const index = hookIndex++;
    if (!(index in slots)) slots[index] = initial;
    return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
  }, useCallback: callback => callback, useEffect: callback => { if (!effects.length) effects.push(callback); } };
  const ownedWindow = { qt: { webChannelTransport: {} }, QWebChannel: class {
    constructor(_transport, callback) { callback({ objects: { bridge: backend } }); }
  } };
  const hookModule = { exports: {} };
  new Function('require', 'module', 'exports', 'window', compiled.outputText)(
    name => name === 'react' ? react : require(name), hookModule, hookModule.exports, ownedWindow);
  const render = () => { hookIndex = 0; return hookModule.exports.useBridge(); };
  render();
  const close = effects[0]();
  assert.equal(backend.executionChanged.callbacks.size, 1);
  requests[0](JSON.stringify(full(1, 1, 100, { ready: true })));
  render().dispatch('ready', { draftDocument: document });
  assert.deepEqual(dispatches[0].payload, { draftDocument: document, streamProtocol: 1, executionProtocol: 1 });
  const sourceContext = render().state.messages[1].metadata.execution.context;
  backend.executionChanged.send(patch());
  assert.equal(render().state.messages[1].metadata.execution.context, sourceContext);
  backend.executionChanged.send(patch(3, 10));
  backend.streamingChanged.send(textPatch(3, 200));
  assert.equal(requests.length, 2);
  requests[1](JSON.stringify(full(3, 9, 199, { ready: true })));
  assert.equal(render().state.messages[1].metadata.execution.events[0].label, 'OWNED working');
  assert.equal(render().state.streamingReply.text, 'Latest text');
  const saved = render().state;
  close();
  assert.equal(backend.executionChanged.callbacks.size, 0);
  backend.executionChanged.send(patch(9, 90));
  requests[1](JSON.stringify(full(9, 90, 900, { ready: true })));
  assert.equal(render().state, saved);
});

test('execution-only early full ordering uses its own available full revision', () => {
  const protocol = new ChatStreamProtocol(false, true);
  protocol.full(full(3, 3, 300, { streamCursor: undefined, title: 'Newest owned full' }));
  protocol.full(full(2, 2, 200, { streamCursor: undefined, title: 'Older owned full' }));
  protocol.initial(full(1, 1, 100, { streamCursor: undefined, title: 'Initial owned full' }));
  assert.equal(protocol.current.executionCursor.revision, 3);
  assert.equal(protocol.current.title, 'Newest owned full');
  assert.equal(protocol.snapshotInFlight, false);
});

test('execution-only early sequence ordering ignores an unnegotiated text sequence', () => {
  const protocol = new ChatStreamProtocol(false, true);
  protocol.full(full(2, 2, 900, { title: 'Older execution with high text sequence' }));
  protocol.full(full(2, 3, 1, { title: 'Latest execution with low text sequence' }));
  protocol.initial(full());
  assert.equal(protocol.current.executionCursor.sequence, 3);
  assert.equal(protocol.current.title, 'Latest execution with low text sequence');
});

const reportName = process.argv[2] ?? 'protocol-first';
assert.match(reportName, /^[a-z0-9-]+$/);
const output = resolve('artifacts/user-testing-20261105/execution-incremental');
mkdirSync(output, { recursive: true });
const report = { scope: 'Actual production bridge protocol with finite owned public packets', passed: tests.length, tests,
  source_sha256: createHash('sha256').update(source).digest('hex'), actual_model_api_calls: 0 };
writeFileSync(resolve(output, reportName + '.json'), JSON.stringify(report, null, 2), { flag: 'wx' });
console.log(JSON.stringify(report));
