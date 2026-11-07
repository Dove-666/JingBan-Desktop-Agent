/** Actual production file-input lifecycle and App handlers; no system clipboard. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const compile = source => {
  const result = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS, strict: true }, reportDiagnostics: true });
  assert.deepEqual(result.diagnostics, []); return result.outputText;
};
const module = { exports: {} };
new Function('require', 'module', 'exports', compile(readFileSync(new URL('../frontend/chat-ui/src/imageInput.ts', import.meta.url), 'utf8')))(require, module, module.exports);
const { ImageInputController, MAX_IMAGE_INPUT_BYTES, hasFileTransfer } = module.exports;
const tests = [];
const failures = [];
async function test(name, action) {
  try { await action(); tests.push(name); }
  catch (error) { failures.push({ name, error: String(error) }); }
}
const file = (extra = {}) => ({ name: 'owned.png', type: 'image/png', size: 3, ...extra });
class Reader {
  result = null; aborts = 0; reads = 0;
  readAsDataURL(value) { this.file = value; this.reads++; }
  abort() { this.aborts++; this.onabort?.(); }
  finish(value = `data:${this.file.type};base64,AAAA`) { this.result = value; this.onload?.(); }
}
function fixture(extra = {}) {
  let context = { conversationId: 'owned-a', draftEpoch: 'epoch-a', draftDocument: 'a'.repeat(32),
    supported: true, available: true, ...extra };
  const readers = [], calls = [], snapshots = [];
  const controller = new ImageInputController({ context: () => context,
    changed: value => snapshots.push(value),
    dispatch: (payload, callback) => { calls.push({ payload, callback }); return true; },
    reader: () => { const value = new Reader(); readers.push(value); return value; } });
  return { controller, calls, readers, snapshots, get context() { return context; },
    setContext: (value, sync = true) => { context = { ...context, ...value }; if (sync) controller.syncContext(); } };
}

await test('valid image stays busy through reader to native ACK; six fields contain raw base64 only', () => {
  const f = fixture(); f.controller.add([file()]);
  assert.equal(f.controller.preparingCurrent(), true); assert.equal(f.controller.busy(), true);
  f.readers[0].finish(); assert.equal(f.controller.busy(), true);
  assert.deepEqual(f.calls[0].payload, { conversationId: 'owned-a', draftEpoch: 'epoch-a', draftDocument: 'a'.repeat(32),
    filename: 'owned.png', mimeType: 'image/png', dataBase64: 'AAAA' });
  f.calls[0].callback({ ok: true }); assert.equal(f.controller.busy(), false);
  assert.equal(f.controller.snapshot().error, undefined);
});

await test('single reader lock rejects duplicate input until ACK', () => {
  const f = fixture(); f.controller.add([file()]); f.controller.add([file()]);
  assert.equal(f.readers.length, 1); f.readers[0].finish(); f.controller.add([file()]);
  assert.equal(f.readers.length, 1); assert.equal(f.calls.length, 1); assert.equal(f.controller.busy(), true);
});

await test('multiple, empty, unsupported and oversize inputs never allocate a FileReader', () => {
  for (const files of [[], [file(), file()], [file({ type: 'image/svg+xml' })],
    [file({ size: MAX_IMAGE_INPUT_BYTES + 1 })], [file({ size: 0 })], [file({ size: NaN })]]) {
    const f = fixture();
    f.controller.add(files); assert.equal(f.readers.length, 0); assert.equal(f.calls.length, 0);
    assert.ok(f.controller.snapshot().error?.message);
  }
});

await test('declared PNG JPEG WebP and exact size boundary admitted; unknown suffix remains rejected', () => {
  for (const type of ['image/png', 'image/jpeg', 'image/webp']) {
    const f = fixture(); f.controller.add([file({ type, size: MAX_IMAGE_INPUT_BYTES })]);
    assert.equal(f.readers.length, 1); f.readers[0].finish(); assert.equal(f.calls[0].payload.mimeType, type);
  }
  const f = fixture(); f.controller.add([file({ type: '', name: 'unknown.bin' })]); assert.equal(f.readers.length, 0);
});

await test('legacy host and unavailable UI cannot allocate a reader or dispatch', () => {
  for (const extra of [{ supported: false }, { available: false }, { draftDocument: '' }, { draftEpoch: '' }]) {
    const f = fixture(extra); f.controller.add([file()]); assert.equal(f.readers.length, 0); assert.equal(f.calls.length, 0);
  }
});

await test('CID, same-CID clear epoch and web document changes abort and reject late load', () => {
  for (const change of [{ conversationId: 'owned-b' }, { draftEpoch: 'cleared-epoch' }, { draftDocument: 'b'.repeat(32) }]) {
    const f = fixture(); f.controller.add([file()]); const late = f.readers[0].onload;
    f.setContext(change); assert.equal(f.readers[0].aborts, 1); assert.equal(f.controller.busy(), false);
    f.readers[0].result = 'data:image/png;base64,AAAA'; late(); assert.equal(f.calls.length, 0);
    assert.equal(f.controller.snapshot().error, undefined);
  }
});

await test('scope is checked on load even before React context effect can abort', () => {
  const f = fixture(); f.controller.add([file()]); f.setContext({ draftEpoch: 'new' }, false);
  f.readers[0].finish(); assert.equal(f.calls.length, 0); assert.equal(f.controller.busy(), false);
});

await test('already-submitted import is not cancelled on CID switch; ACK lock remains global and local only to source', () => {
  const f = fixture(); f.controller.add([file()]); f.readers[0].finish();
  f.setContext({ conversationId: 'owned-b', draftEpoch: 'epoch-b' });
  assert.equal(f.readers[0].aborts, 0); assert.equal(f.controller.busy(), true);
  assert.equal(f.controller.preparingCurrent(), false);
  f.calls[0].callback({ ok: false, error: 'OWNED_PRIVATE_HOST_ERROR' });
  assert.equal(f.controller.busy(), false); assert.equal(f.controller.snapshot().error, undefined);
  assert.equal(f.calls.length, 1);
});

await test('same-CID clear/reload before ACK cannot set new-scope errors or start another import', () => {
  for (const change of [{ draftEpoch: 'new' }, { draftDocument: 'b'.repeat(32) }]) {
    const f = fixture(); f.controller.add([file()]); f.readers[0].finish(); f.setContext(change);
    assert.equal(f.controller.preparingCurrent(), false); assert.equal(f.controller.busy(), true);
    f.calls[0].callback({ ok: false }); assert.equal(f.controller.snapshot().error, undefined);
    assert.equal(f.calls.length, 1);
  }
});

await test('unmount aborts reading and ignores late callbacks; StrictMode activation accepts a fresh job', () => {
  const f = fixture(); f.controller.add([file()]); const late = f.readers[0].onload;
  f.controller.dispose(); const count = f.snapshots.length; f.readers[0].result = 'data:image/png;base64,AAAA'; late();
  assert.equal(f.readers[0].aborts, 1); assert.equal(f.snapshots.length, count); assert.equal(f.calls.length, 0);
  f.controller.activate(); f.controller.add([file()]); assert.equal(f.readers.length, 2);
});

await test('unmount during ACK preserves admitted native ownership and suppresses all late state writes', () => {
  const f = fixture(); f.controller.add([file()]); f.readers[0].finish(); f.controller.dispose();
  const count = f.snapshots.length; f.calls[0].callback({ ok: false });
  assert.equal(f.readers[0].aborts, 0); assert.equal(f.snapshots.length, count); assert.equal(f.calls.length, 1);
});

await test('filename bound uses 160 Unicode codepoints and removes controls/separators', () => {
  const f = fixture(); f.controller.add([file({ name: '../🐳'.repeat(180) + '\u0000.png' })]); f.readers[0].finish();
  const name = f.calls[0].payload.filename; assert.equal(Array.from(name).length, 160);
  assert.equal(/[\x00-\x1f/\\]/.test(name), false); assert.equal(name.includes('\ufffd'), false);
});

await test('reader failures and malformed results show fixed errors without dispatch or private text', () => {
  for (const result of ['data:image/jpeg;base64,AAAA', 'data:image/png;base64,not_base64', new ArrayBuffer(2), 'data:image/png;base64,']) {
    const f = fixture(); f.controller.add([file()]); f.readers[0].finish(result);
    assert.equal(f.calls.length, 0); assert.equal(f.controller.snapshot().error.message, '图片未能添加，请重试。');
  }
  const f = fixture(); f.controller.add([file()]); f.readers[0].onerror({ message: 'OWNED_SECRET' });
  assert.equal(f.controller.snapshot().error.message.includes('OWNED_SECRET'), false);
});

const appSource = readFileSync(new URL('../frontend/chat-ui/src/App.tsx', import.meta.url), 'utf8');
const ast = ts.createSourceFile('App.tsx', appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function actualArrow(name, unwrap = false) {
  let found;
  function visit(node) { if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) found = node.initializer;
    ts.forEachChild(node, visit); }
  visit(ast); assert.ok(found, `Missing production ${name}`);
  if (unwrap) found = found.arguments[0];
  assert.equal(ts.isArrowFunction(found), true);
  return found.getText(ast);
}
const expression = (name, scope, unwrap = false) => {
  const fnSource = compile(`const actual = ${actualArrow(name, unwrap)}; exports.actual = actual;`);
  const m = { exports: {} }; new Function('exports', ...Object.keys(scope), fnSource)(m.exports, ...Object.values(scope));
  return m.exports.actual;
};

await test('actual App paste/drop handlers preserve pure text and only consume event FileList', () => {
  const added = [], imageInput = { current: { add: files => added.push(files) } };
  const paste = expression('pasteImage', { imageInput });
  const drop = expression('dropImage', { imageInput, hasFileTransfer });
  let prevented = 0;
  paste({ clipboardData: { files: [] }, preventDefault: () => prevented++ });
  drop({ dataTransfer: { files: [], types: ['text/plain'] }, preventDefault: () => prevented++ });
  assert.equal(prevented, 0); assert.equal(added.length, 0);
  const files = [file()]; paste({ clipboardData: { files }, preventDefault: () => prevented++ });
  drop({ dataTransfer: { files, types: ['Files'] }, preventDefault: () => prevented++ });
  assert.equal(prevented, 2); assert.equal(added.length, 2); assert.equal(added[0], files); assert.equal(added[1], files);
  const preventNavigation = expression('preventFileNavigation', { hasFileTransfer });
  preventNavigation({ dataTransfer: { files, types: ['Files'] }, preventDefault: () => prevented++ });
  preventNavigation({ dataTransfer: { files: [], types: ['text/plain'] }, preventDefault: () => prevented++ });
  assert.equal(prevented, 3); assert.equal(added.length, 2);
});

await test('actual submit callback immediately guards FileReader job before React has rerendered', async () => {
  const f = fixture(); f.controller.add([file()]);
  const submit = expression('submitMessage', { stagedImage: undefined, state: { busy: false, historyLoading: false },
    canSteer: false, imagePreparing: false, imageInput: { current: f.controller }, pending: null }, true);
  await submit('preserved draft'); assert.equal(f.calls.length, 0);
});

await test('actual bridge capability normalization defaults false and requires boolean true', () => {
  const bridgeModule = { exports: {} };
  new Function('require', 'module', 'exports', compile(readFileSync(new URL('../frontend/chat-ui/src/bridge.ts', import.meta.url), 'utf8')))(require, bridgeModule, bridgeModule.exports);
  for (const [value, expected] of [[undefined, false], [false, false], ['true', false], [1, false], [true, true]]) {
    const protocol = new bridgeModule.exports.ChatStreamProtocol(false);
    protocol.initial({ conversationId: 'owned-a', messages: [], conversations: [], canStageImageData: value });
    assert.equal(protocol.current.canStageImageData, expected);
  }
});

await test('temporary duplicate-input busy error disappears after the original image succeeds', () => {
  const f = fixture(); f.controller.add([file()]); f.controller.add([file()]);
  assert.equal(f.controller.snapshot().error.message, '正在处理，请稍后再添加图片。');
  f.readers[0].finish(); f.calls[0].callback({ ok: true });
  assert.equal(f.controller.busy(), false); assert.equal(f.controller.snapshot().error, undefined);
});

await test('actual form send guard rejects same-turn paste before assistant-ui optimistic append', () => {
  const f = fixture(); f.controller.add([file()]); const appends = [];
  const send = expression('send', { canSend: true, composition: { current: false },
    imageInput: { current: f.controller }, state: { busy: false }, canSteer: false,
    draft: 'preserved text', stagedImage: undefined, runtime: { thread: { append: value => appends.push(value) } },
    editor: { current: null } });
  send({ preventDefault() {} }); assert.equal(appends.length, 0);
});

await test('empty or generic File type with explicit image extension uses a bounded typed Blob of the same bytes', async () => {
  for (const type of ['', 'application/octet-stream']) for (const [extension, expected] of [['png', 'image/png'], ['JPG', 'image/jpeg'], ['jpeg', 'image/jpeg'], ['webp', 'image/webp']]) {
    const input = new File([Uint8Array.of(1, 2, 3)], `owned.${extension}`, { type });
    const f = fixture(); f.controller.add([input]); assert.equal(f.readers.length, 1);
    const actual = f.readers[0].file; assert.ok(actual instanceof Blob); assert.equal(actual.type, expected);
    assert.equal(actual.size, input.size); assert.deepEqual(new Uint8Array(await actual.arrayBuffer()), Uint8Array.of(1, 2, 3));
    f.readers[0].finish(); assert.equal(f.calls[0].payload.mimeType, expected);
  }
});

await test('empty/generic unknown suffix and explicit SVG GIF PDF remain rejected without reading', () => {
  for (const [type, name] of [['', 'unknown.bin'], ['application/octet-stream', 'unknown.bin'], ['image/svg+xml', 'owned.png'],
    ['image/gif', 'owned.png'], ['application/pdf', 'owned.png']]) {
    const f = fixture(); f.controller.add([new File([Uint8Array.of(1)], name, { type })]);
    assert.equal(f.readers.length, 0); assert.equal(f.calls.length, 0);
  }
});

console.log(JSON.stringify({ scope: 'Production ImageInputController, AST-extracted App callbacks and actual bridge normalizer; memory-only event files/FakeReader; no native/system clipboard/models',
  passed: tests.length, failed: failures.length, tests, failures }, null, 2));
assert.equal(failures.length, 0, 'Image input regressions failed');
