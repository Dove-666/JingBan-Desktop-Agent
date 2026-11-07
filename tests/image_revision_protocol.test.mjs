/** Production bridge and extracted production attachment receiver, no network. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const bridgeSource = readFileSync(new URL('../frontend/chat-ui/src/bridge.ts', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../frontend/chat-ui/src/App.tsx', import.meta.url), 'utf8');
const compile = source => {
  const result = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }, reportDiagnostics: true });
  assert.deepEqual(result.diagnostics, []);
  return result.outputText;
};
const compiled = compile(bridgeSource);
const bridgeModule = { exports: {} };
new Function('require', 'module', 'exports', compiled)(require, bridgeModule, bridgeModule.exports);
const bridge = bridgeModule.exports;
const tests = [], failures = [];
function test(name, action) {
  try { action(); tests.push(name); }
  catch (error) { failures.push({ name, error: String(error) }); }
}
const hash = 'a'.repeat(64);
const ready = extra => ({ conversationId: 'owned', messageId: 'picture', imageId: hash, ...extra });
const thumbnail = { width: 24, height: 16,
  src: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAAQCAYAAAAMJL+VAAAACXBIWXMAAA7EAAAOxAGVKw4bAAAAIUlEQVQ4jWMs3fHlPwMNARMtDR+1YNSCUQtGLRi1gFoAAFFwA0AQManbAAAAAElFTkSuQmCC' };

function hookFixture() {
  const slots = [], effects = [], events = [];
  let index = 0;
  const signal = () => {
    const listeners = new Set();
    return { connect: callback => listeners.add(callback), disconnect: callback => listeners.delete(callback),
      emit: value => [...listeners].forEach(callback => callback(JSON.stringify(value))) };
  };
  const backend = { getState: () => {}, dispatch: (_a, _p, cb) => cb?.('{"ok":true}'),
    stateChanged: signal(), streamingChanged: signal(), thumbnailReady: signal() };
  const react = { useState: initial => {
    const slot = index++;
    if (!(slot in slots)) slots[slot] = initial;
    return [slots[slot], value => { slots[slot] = typeof value === 'function' ? value(slots[slot]) : value; }];
  }, useCallback: value => value, useEffect: action => { if (!effects.length) effects.push(action); } };
  const window = { qt: { webChannelTransport: {} }, dispatchEvent: event => events.push(event),
    QWebChannel: class { constructor(_t, cb) { cb({ objects: { bridge: backend } }); } } };
  class OwnedEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } }
  const owned = { exports: {} };
  new Function('require', 'module', 'exports', 'window', 'CustomEvent', compiled)(
    name => name === 'react' ? react : require(name), owned, owned.exports, window, OwnedEvent);
  owned.exports.useBridge();
  const cleanup = effects[0]();
  return { backend, events, cleanup };
}

function attachmentReceiver(revision) {
  const parsed = ts.createSourceFile('App.tsx', appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const node = parsed.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === 'MessageImageAttachment');
  assert.ok(node);
  const source = `${node.getText(parsed)}\nmodule.exports.component=MessageImageAttachment;`;
  const refs = [], effects = [], changes = [];
  let stateIndex = 0;
  const useRef = initial => { const value = { current: initial }; refs.push(value); return value; };
  const useState = initial => {
    const index = stateIndex++;
    return [initial, value => changes.push({ index, value })];
  };
  let listener;
  const window = { addEventListener: (_name, callback) => { listener = callback; }, removeEventListener: () => {} };
  const owned = { exports: {} };
  new Function('require', 'module', 'exports', 'useContext', 'useRef', 'useState', 'useCallback', 'useEffect',
    'HostContext', 'normalizeThumbnailError', 'normalizeImageThumbnail', 'normalizeImageRevision',
    'IMAGE_THUMBNAIL_EVENT', 'window', compile(source))(
    require, owned, owned.exports, () => ({ dispatch: () => {} }), useRef, useState, value => value,
    action => effects.push(action), {}, bridge.normalizeThumbnailError, bridge.normalizeImageThumbnail,
    bridge.normalizeImageRevision, bridge.IMAGE_THUMBNAIL_EVENT, window);
  owned.exports.component({ conversationId: 'owned', messageId: 'picture', imageId: hash, imageRevision: revision });
  refs[2].current = true; // Existing production visibleNow ref, owned observer state.
  const cleanup = effects[2](); // Actual event receiver effect, not an equivalent implementation.
  return { changes, receive: value => listener({ detail: value }), cleanup };
}

test('revision accepts legacy absence and safe integer endpoints', () => {
  assert.equal(typeof bridge.normalizeImageRevision, 'function');
  for (const [input, expected] of [[undefined, 0], [0, 0], [1, 1], [Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER]]) {
    assert.equal(bridge.normalizeImageRevision(input), expected);
  }
});
test('revision rejects explicit malformed values', () => {
  for (const value of [null, false, true, '', '1', -1, .5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, {}, []]) {
    assert.equal(bridge.normalizeImageRevision(value), undefined);
  }
});
test('actual full-state normalization carries bounded revision and legacy zero', () => {
  for (const [value, expected] of [[undefined, 0], [7, 7], ['7', 0], [-1, 0]]) {
    const p = new bridge.ChatStreamProtocol(false);
    p.initial({ conversationId: 'owned', conversations: [], messages: [{ id: 'picture', role: 'user', content: '',
      metadata: { image_id: hash, image_revision: value } }] });
    assert.equal(p.current.messages[0].metadata.image_revision, expected);
  }
});
test('actual bridge normalizes legacy ready and discards malformed revisions', () => {
  const f = hookFixture();
  try {
    f.backend.thumbnailReady.emit(ready({ thumbnail }));
    assert.equal(f.events.length, 1);
    assert.equal(f.events[0].detail.imageRevision, 0);
    for (const value of [null, false, '1', -1, .5, Number.MAX_SAFE_INTEGER + 1]) {
      f.backend.thumbnailReady.emit(ready({ imageRevision: value, thumbnail }));
    }
    assert.equal(f.events.length, 1);
    f.backend.thumbnailReady.emit(ready({ imageRevision: 9, thumbnail }));
    assert.equal(f.events.at(-1).detail.imageRevision, 9);
  } finally { f.cleanup(); }
});
test('actual attachment receiver rejects older ready for the same image identity', () => {
  const f = attachmentReceiver(2);
  try {
    f.receive(ready({ imageRevision: 1, thumbnail }));
    f.receive(ready({ imageRevision: 0, error: 'old missing error' }));
    assert.equal(f.changes.length, 0);
    f.receive(ready({ imageRevision: 2, thumbnail }));
    assert.ok(f.changes.some(change => change.index === 1 && change.value?.src === thumbnail.src));
  } finally { f.cleanup(); }
});
test('actual attachment receiver keeps legacy epoch-zero ready support', () => {
  const f = attachmentReceiver(0);
  try {
    f.receive(ready({ thumbnail }));
    assert.ok(f.changes.some(change => change.index === 1 && change.value?.src === thumbnail.src));
  } finally { f.cleanup(); }
});
test('production attachment key includes revision and runtime metadata carries it', () => {
  assert.ok(/key=\{`[^`]*meta\.image_revision/.test(appSource), 'attachment key must carry revision');
  assert.ok(/image_revision:\s*item\.metadata\?\.image_revision/.test(appSource), 'runtime metadata must carry revision');
});
process.stdout.write(JSON.stringify({ passed: !failures.length, tests: tests.length + failures.length,
  passing: tests, failures, scope: 'Production bridge/hook and AST-extracted actual attachment receiver. Hook fixtures emulate visibility; rendered component reset is separately verified in compiled Qt.' }, null, 2) + '\n');
process.exitCode = failures.length ? 1 : 0;
