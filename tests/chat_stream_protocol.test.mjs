/** Actual production bridge protocol, transpiled without a renderer/model. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../frontend/chat-ui/src/bridge.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, strict: true,
}, reportDiagnostics: true });
assert.deepEqual(compiled.diagnostics, []);
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled.outputText)(require, module, module.exports);
const { ChatStreamProtocol: StreamProtocol, normalizeStreamCursor: normalizeCursor } = module.exports;
const document = 'a'.repeat(32), otherDocument = 'b'.repeat(32);
const reply = (requestId = '1', text = 'first', cid = 'current', state = 'streaming') =>
  ({ conversationId: cid, requestId, text, state });
const full = (revision = 1, sequence = 1, extra = {}) => ({
  conversationId: 'current', draftDocument: document,
  messages: [{ id: 'm1', role: 'user', content: 'owned' }], conversations: [],
  busy: true, requestInCurrent: true, streamingReply: reply(),
  streamCursor: { version: 1, document, revision, sequence }, ...extra,
});
const patch = (revision = 1, sequence = 2, extra = {}) => ({
  version: 1, document, conversationId: 'current', revision, sequence,
  streamingReply: reply('1', 'second'), ...extra,
});
const ready = () => { const protocol = new StreamProtocol(true); protocol.initial(full()); return protocol; };
const tests = [];
function test(name, action) { action(); tests.push(name); }

test('pure text patch preserves all history/sidebar references', () => {
  const p = ready(), previous = p.current;
  const outcome = p.patch(patch());
  assert.equal(outcome.changed, true);
  assert.equal(p.current.messages, previous.messages);
  assert.equal(p.current.conversations, previous.conversations);
  assert.equal(p.current.streamingReply.text, 'second');
  assert.equal(p.patch(patch()).changed, false);
});
test('stale full callback cannot roll back newer sequence', () => {
  const p = ready(); p.patch(patch(1, 3));
  assert.equal(p.full(full(1, 2)).changed, false);
  assert.equal(p.current.streamingReply.text, 'second');
});
test('future patch buffers one and recovery applies full then latest text', () => {
  const p = ready();
  const first = p.patch(patch(3, 5, { streamingReply: reply('3', 'fifth') }));
  assert.equal(first.requestSnapshot, 1);
  for (let sequence = 6; sequence <= 100; sequence++) {
    assert.equal(p.patch(patch(3, sequence, { streamingReply: reply('3', 'delta-' + sequence) })).requestSnapshot, undefined);
  }
  assert.equal(p.bufferedPatches, 1);
  const outcome = p.recovery(1, full(3, 4, { streamingReply: reply('3', 'fourth') }));
  assert.equal(outcome.changed, true);
  assert.equal(p.current.streamingReply.text, 'delta-100');
  assert.equal(p.bufferedPatches, 0);
  assert.equal(p.snapshotInFlight, false);
});
test('stale recovery never feeds a repeated getState feedback storm', () => {
  const p = ready(), first = p.patch(patch(3, 10));
  assert.equal(p.recovery(first.requestSnapshot, full(1, 1)).requestSnapshot, undefined);
  for (let sequence = 11; sequence < 100; sequence++)
    assert.equal(p.patch(patch(3, sequence)).requestSnapshot, undefined);
  assert.equal(p.current.streamCursor.revision, 1);
});
test('strict current revision rejects foreign CID/document and request', () => {
  const p = ready(), before = p.current;
  for (const extra of [
    { document: otherDocument },
    { conversationId: 'other', streamingReply: reply('1', 'private', 'other') },
    { streamingReply: reply('old', 'private') },
  ]) assert.equal(p.patch(patch(1, 30, extra)).changed, false);
  assert.equal(p.current, before);
});
test('same CID new request and Stop block late old request packets', () => {
  const p = ready(); p.full(full(2, 2, { streamingReply: reply('2', 'new') }));
  assert.equal(p.patch(patch(1, 100)).changed, false);
  assert.equal(p.patch(patch(2, 101)).changed, false);
  p.full(full(3, 3, { busy: false, streamingReply: reply('2', 'new', 'current', 'interrupted') }));
  assert.equal(p.patch(patch(3, 102, { streamingReply: reply('2', 'late') })).changed, false);
  assert.equal(p.current.streamingReply.state, 'interrupted');
});
test('clear final full cannot resurrect a preview', () => {
  const p = ready(); p.full(full(2, 3, { busy: false, streamingReply: undefined }));
  assert.equal(p.patch(patch(2, 100)).changed, false);
  assert.equal(p.current.streamingReply, undefined);
});
test('future CID patch waits for matching new conversation snapshot', () => {
  const p = ready();
  const update = patch(2, 4, { conversationId: 'next', streamingReply: reply('2', 'next text', 'next') });
  const { requestSnapshot } = p.patch(update);
  assert.equal(p.current.conversationId, 'current');
  p.recovery(requestSnapshot, full(2, 3, { conversationId: 'next', streamingReply: reply('2', 'first next', 'next') }));
  assert.equal(p.current.conversationId, 'next');
  assert.equal(p.current.streamingReply.text, 'next text');
});
test('initial getState alone anchors document; early wrong document is discarded', () => {
  const p = new StreamProtocol(true);
  p.full(full(100, 100, { draftDocument: otherDocument,
    streamCursor: { version: 1, document: otherDocument, revision: 100, sequence: 100 } }));
  p.initial(full());
  assert.equal(p.current.draftDocument, document);
  assert.equal(p.current.streamCursor.revision, 1);
  assert.equal(p.full(full(101, 101, { draftDocument: otherDocument })).changed, false);
});
test('early foreign revision cannot hide current final state behind delayed initial read', () => {
  const p = new StreamProtocol(true);
  p.full(full(100, 100, { draftDocument: otherDocument,
    streamCursor: { version: 1, document: otherDocument, revision: 100, sequence: 100 } }));
  p.full(full(2, 2, { busy: false, streamingReply: undefined,
    messages: [{ id: 'final', role: 'assistant', content: 'owned completed answer' }] }));
  const initial = p.initial(full());
  assert.equal(p.current.draftDocument, document);
  assert.equal(p.current.messages.some(message => message.content === 'owned completed answer'), false);
  assert.equal(initial.requestSnapshot, 1);
  const recovered = p.recovery(initial.requestSnapshot, full(3, 2, { busy: false,
    streamingReply: undefined, messages: [{ id: 'final', role: 'assistant', content: 'owned completed answer' }] }));
  assert.equal(recovered.requestSnapshot, undefined);
  assert.equal(p.current.busy, false);
  assert.equal(p.current.messages[0].content, 'owned completed answer');
  assert.equal(p.current.streamingReply, undefined);
  assert.equal(p.snapshotInFlight, false);
});
test('foreign initial recovery reuses an existing pending patch ticket', () => {
  const p = new StreamProtocol(true);
  p.full(full(100, 100, { draftDocument: otherDocument,
    streamCursor: { version: 1, document: otherDocument, revision: 100, sequence: 100 } }));
  p.patch(patch(3, 5, { streamingReply: reply('3', 'fifth') }));
  const initial = p.initial(full());
  assert.equal(initial.requestSnapshot, 1);
  for (let sequence = 6; sequence <= 20; sequence++)
    assert.equal(p.patch(patch(3, sequence, { streamingReply: reply('3', 'latest') })).requestSnapshot, undefined);
  p.recovery(initial.requestSnapshot, full(3, 4, { streamingReply: reply('3', 'fourth') }));
  assert.equal(p.current.streamingReply.text, 'latest');
  assert.equal(p.snapshotInFlight, false);
});
test('stale foreign-initial recovery does not retry or poison a later real gap', () => {
  const p = new StreamProtocol(true);
  p.full(full(100, 100, { draftDocument: otherDocument,
    streamCursor: { version: 1, document: otherDocument, revision: 100, sequence: 100 } }));
  const initial = p.initial(full());
  assert.equal(initial.requestSnapshot, 1);
  assert.equal(p.recovery(initial.requestSnapshot, full()).requestSnapshot, undefined);
  for (let revision = 101; revision <= 120; revision++) {
    assert.equal(p.full(full(revision, revision, { draftDocument: otherDocument })).requestSnapshot, undefined);
    assert.equal(p.patch(patch(revision, revision, { document: otherDocument })).requestSnapshot, undefined);
  }
  assert.equal(p.snapshotInFlight, false);
  assert.equal(p.patch(patch(2, 2)).requestSnapshot, 2);
});
test('foreign initial recovery preserves full-state-only capability fallback', () => {
  const p = new StreamProtocol(false);
  p.full(full(100, 100, { draftDocument: otherDocument,
    streamCursor: { version: 1, document: otherDocument, revision: 100, sequence: 100 } }));
  const initial = p.initial(full(1, 1, { streamCursor: undefined }));
  assert.equal(p.negotiatedCapability, undefined);
  assert.equal(initial.requestSnapshot, 1);
  p.recovery(initial.requestSnapshot, full(2, 2, { streamCursor: undefined,
    busy: false, streamingReply: undefined }));
  assert.equal(p.current.busy, false);
  assert.equal(p.negotiatedCapability, undefined);
  assert.equal(p.patch(patch(2, 3)).changed, false);
  assert.equal(p.snapshotInFlight, false);
});
test('one early full and stream survives delayed initial getState callback', () => {
  const p = new StreamProtocol(true);
  p.full(full(2, 3, { streamingReply: reply('2', 'third') }));
  p.patch(patch(2, 4, { streamingReply: reply('2', 'fourth') }));
  p.initial(full());
  assert.equal(p.current.streamingReply.text, 'fourth');
});
test('initial replay exposes exactly one recovery request for an early future gap', () => {
  const p = new StreamProtocol(true);
  p.full(full(2, 3, { streamingReply: reply('2', 'third') }));
  p.patch(patch(3, 5, { streamingReply: reply('3', 'fifth') }));
  const initial = p.initial(full());
  assert.equal(initial.requestSnapshot, 1);
  assert.equal(p.current.streamCursor.revision, 2);
  assert.equal(p.patch(patch(3, 6, { streamingReply: reply('3', 'sixth') })).requestSnapshot, undefined);
  p.recovery(1, full(3, 4, { streamingReply: reply('3', 'fourth') }));
  assert.equal(p.current.streamingReply.text, 'sixth');
});
test('missing signal or unknown/absent protocol retains full-state-only route', () => {
  for (const [signal, cursor] of [[false, full().streamCursor], [true, undefined], [true, { version: 2 }]]) {
    const p = new StreamProtocol(signal);
    p.initial(full(1, 1, { streamCursor: cursor }));
    assert.equal(p.negotiatedCapability, undefined);
    assert.equal(p.patch(patch()).changed, false);
    assert.equal(p.full(full(2, 2, { streamingReply: reply('2', 'legacy full') })).changed, true);
    assert.equal(p.current.streamingReply.text, 'legacy full');
  }
});
test('global image import busy is separate from current conversation preparation', () => {
  const p = new StreamProtocol(true);
  p.initial(full(1, 1, { imagePreparing: false, imageImportBusy: true }));
  assert.equal(p.current.imagePreparing, false);
  assert.equal(p.current.imageImportBusy, true);
  p.patch(patch(1, 2));
  assert.equal(p.current.imageImportBusy, true);
  p.full(full(2, 2, { imagePreparing: false, imageImportBusy: false }));
  assert.equal(p.current.imageImportBusy, false);
});
test('legacy image import busy falls back to the existing preparation flag', () => {
  for (const preparing of [false, true]) {
    for (const busy of [undefined, 'invalid']) {
      const p = new StreamProtocol(false);
      p.initial(full(1, 1, { imagePreparing: preparing, imageImportBusy: busy }));
      assert.equal(p.current.imageImportBusy, preparing);
    }
  }
});
test('safe numeric and reply validation prevents malformed frame application', () => {
  assert.equal(normalizeCursor({ version: 1, document, revision: 1.2, sequence: 2 }), undefined);
  assert.equal(normalizeCursor({ version: 1, document, revision: 1, sequence: Number.MAX_SAFE_INTEGER + 1 }), undefined);
  const p = ready(), before = p.current;
  for (const extra of [{ streamingReply: null }, { streamingReply: reply('<unsafe>', 'no') },
    { streamingReply: reply('1', 'x'.repeat(1048577)) }, { streamingReply: reply('1', 'no', 'current', 'interrupted') },
    { revision: NaN }, { sequence: -1 }]) assert.equal(p.patch(patch(1, 5, extra)).changed, false);
  assert.equal(p.current, before);
});
test('dispose denies all owned late callback/event paths', () => {
  const p = ready(), { requestSnapshot } = p.patch(patch(4, 10)), before = p.current;
  p.dispose();
  assert.equal(p.recovery(requestSnapshot, full(4, 9)).changed, false);
  assert.equal(p.full(full(5, 11)).changed, false);
  assert.equal(p.patch(patch(5, 12)).changed, false);
  assert.equal(p.current, before);
  assert.equal(p.bufferedPatches, 0);
});
test('old backend without document nonce still accepts full snapshots', () => {
  const p = new StreamProtocol(false), first = full(1, 1, { streamCursor: undefined });
  delete first.draftDocument;
  p.initial(first);
  assert.equal(p.current.draftDocument, '');
  assert.equal(p.full({ ...first, title: 'legacy updated' }).changed, true);
  assert.equal(p.current.title, 'legacy updated');
});
test('outdated snapshot is rejected before traversing its messages', () => {
  const p = ready();
  const history = new Proxy([], { get(target, key) {
    if (key === 'map') throw new Error('stale history was traversed');
    return Reflect.get(target, key);
  } });
  assert.equal(p.full(full(0, 0, { messages: history })).changed, false);
});

function hookFixture({ signal = true } = {}) {
  const slots = [], effects = [], requests = [], dispatches = [];
  let index = 0, cleaned = false;
  const event = () => {
    const callbacks = new Set();
    return { connect: callback => callbacks.add(callback), disconnect: callback => callbacks.delete(callback),
      send: value => [...callbacks].forEach(callback => callback(JSON.stringify(value))), callbacks };
  };
  const backend = { getState: callback => requests.push(callback),
    dispatch: (action, payload, callback) => { dispatches.push({ action, payload: JSON.parse(payload) }); callback?.('{"ok":true}'); },
    stateChanged: event(), ...(signal ? { streamingChanged: event() } : {}), thumbnailReady: event() };
  const react = { useState: initial => {
    const slot = index++;
    if (!(slot in slots)) slots[slot] = initial;
    return [slots[slot], value => { slots[slot] = typeof value === 'function' ? value(slots[slot]) : value; }];
  }, useCallback: action => action, useEffect: action => { if (!effects.length) effects.push(action); } };
  const window = { qt: { webChannelTransport: {} }, QWebChannel: class {
    constructor(_transport, callback) { callback({ objects: { bridge: backend } }); }
  } };
  const ownedModule = { exports: {} };
  new Function('require', 'module', 'exports', 'window', compiled.outputText)(
    name => name === 'react' ? react : require(name), ownedModule, ownedModule.exports, window);
  const render = () => { index = 0; return ownedModule.exports.useBridge(); };
  render(); const cleanup = effects[0]();
  return { backend, requests, dispatches, render, close: () => { if (!cleaned) { cleaned = true; cleanup(); } } };
}
test('actual hook attaches signal before nonce capability acknowledgement', () => {
  const f = hookFixture();
  try {
    assert.equal(f.requests.length, 1);
    assert.equal(f.backend.streamingChanged.callbacks.size, 1);
    f.requests[0](JSON.stringify(full(1, 1, { ready: true })));
    const view = f.render();
    view.dispatch('ready', { draftDocument: document });
    assert.deepEqual(f.dispatches[0], { action: 'ready', payload: { draftDocument: document, streamProtocol: 1 } });
    view.dispatch('ready', {});
    assert.deepEqual(f.dispatches[1].payload, {});
  } finally { f.close(); }
});
test('actual hook recovers current final state hidden by an early foreign full once', () => {
  const f = hookFixture();
  try {
    f.backend.stateChanged.send(full(100, 100, { draftDocument: otherDocument,
      streamCursor: { version: 1, document: otherDocument, revision: 100, sequence: 100 } }));
    f.backend.stateChanged.send(full(2, 2, { busy: false, streamingReply: undefined,
      messages: [{ id: 'final', role: 'assistant', content: 'owned completed answer' }] }));
    f.requests[0](JSON.stringify(full(1, 1, { ready: true })));
    assert.equal(f.requests.length, 2);
    f.requests[1](JSON.stringify(full(3, 2, { ready: true, busy: false, streamingReply: undefined,
      messages: [{ id: 'final', role: 'assistant', content: 'owned completed answer' }] })));
    assert.equal(f.render().state.busy, false);
    assert.equal(f.render().state.messages[0].content, 'owned completed answer');
    assert.equal(f.requests.length, 2);
  } finally { f.close(); }
});
test('actual hook preserves history, performs one gap recovery, and denies late teardown callbacks', () => {
  const f = hookFixture();
  f.requests[0](JSON.stringify(full(1, 1, { ready: true })));
  const history = f.render().state.messages;
  f.backend.streamingChanged.send(patch());
  assert.equal(f.render().state.messages, history);
  f.backend.streamingChanged.send(patch(3, 5, { streamingReply: reply('3', 'fifth') }));
  f.backend.streamingChanged.send(patch(3, 6, { streamingReply: reply('3', 'sixth') }));
  assert.equal(f.requests.length, 2);
  f.requests[1](JSON.stringify(full(3, 4, { ready: true, streamingReply: reply('3', 'fourth') })));
  assert.equal(f.render().state.streamingReply.text, 'sixth');
  const saved = f.render().state;
  f.close();
  assert.equal(f.backend.streamingChanged.callbacks.size, 0);
  f.requests[1](JSON.stringify(full(8, 20, { streamingReply: reply('8', 'late') })));
  assert.equal(f.render().state, saved);
});
test('actual hook old backend uses no capability and invalid initial state reports error', () => {
  const old = hookFixture({ signal: false });
  try {
    old.requests[0](JSON.stringify(full(1, 1, { ready: true, streamCursor: undefined })));
    old.render().dispatch('ready', { draftDocument: document });
    assert.deepEqual(old.dispatches[0].payload, { draftDocument: document });
  } finally { old.close(); }
  const invalid = hookFixture();
  try {
    invalid.requests[0]('{"invalid":true}');
    assert.notEqual(invalid.render().connectionError, '');
    assert.equal(invalid.requests.length, 1);
  } finally { invalid.close(); }
});
console.log(JSON.stringify({ scope: 'actual production bridge pure protocol', file: fileURLToPath(import.meta.url),
  passed: tests.length, tests, actual_model_api_calls: 0 }));
