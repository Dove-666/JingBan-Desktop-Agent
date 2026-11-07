/** Request ownership through the actual App callback and bridge normalizer. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../frontend/chat-ui/src/App.tsx', import.meta.url), 'utf8');
const bridge = readFileSync(new URL('../frontend/chat-ui/src/bridge.ts', import.meta.url), 'utf8');
const tree = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let callback;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'historyMessages') callback = node.initializer.arguments[0];
  ts.forEachChild(node, visit);
}
visit(tree);
assert.ok(callback);
const text = ts.createPrinter().printNode(ts.EmitHint.Expression, callback, tree);
const compiled = ts.transpileModule('const callback=' + text, { compilerOptions: { target: ts.ScriptTarget.ES2022 } });
const convert = new Function('state', 'convertedMessageCache', compiled.outputText + '\nreturn callback();');
const bridgeModule = { exports: {} };
const normalizedModule = ts.transpileModule(bridge, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
new Function('require', 'module', 'exports', normalizedModule.outputText + '\nexports.ownedNormalize=normalizeChatState;')(require, bridgeModule, bridgeModule.exports);
const normalize = bridgeModule.exports.ownedNormalize;
const started = '2026-11-06T03:00:00+00:00';
const trace = (label, state = 'completed', time = started) => ({ version: 1, state, started_at: time, duration_ms: 1000,
  events: [{ id: label, kind: 'tool', label: 'Tool ' + label, elapsed_ms: 100, status: 'completed' }],
  context: { model: 'owned', policy: 'model', messages: [{ role: 'user', content: 'Context ' + label }] } });
const missing = Symbol('missing');
function row(id, role, execution, link = missing, source = role) {
  return { id, role, source, content: id, metadata: { ...(execution ? { execution } : {}),
    ...(link === missing ? {} : { reply_to: link }) } };
}
const rendered = (rows) => convert({ messages: rows }, { current: new WeakMap() });
const owners = (rows) => rendered(rows).filter(item => item.metadata.custom.execution).map(item => item.id);
const tests = [];
function test(name, action) {
  try { action(); tests.push({ name, passed: true }); }
  catch (error) { tests.push({ name, passed: false, error: error.stack }); }
}
test('actual Store two-pair same-start archive keeps both distinct contexts', () => {
  const rows = JSON.parse(readFileSync(new URL('../artifacts/user-testing-20261105/same-time-trace-readonly/actual-store-rows.json', import.meta.url), 'utf8'));
  const result = rendered(rows).filter(item => item.metadata.custom.execution);
  assert.deepEqual(result.map(item => item.id), rows.filter(item => item.role === 'assistant').map(item => item.id));
  assert.deepEqual(result.map(item => item.metadata.custom.execution.context.messages[0].content), ['独立上下文 A', '独立上下文 B']);
});
test('queued explicit reply links preserve user-only trace inheritance', () => {
  const rows = [row('u1', 'user', trace('A')), row('u2', 'user', trace('B')),
    row('a1', 'assistant', undefined, 'u1'), row('a2', 'assistant', trace('B'), 'u2')];
  const result = rendered(rows).filter(item => item.metadata.custom.execution);
  assert.deepEqual(result.map(item => item.id), ['a1', 'a2']);
  assert.deepEqual(result.map(item => item.metadata.custom.execution.context.messages[0].content), ['Context A', 'Context B']);
});
test('null and explicit invalid link values never become legacy adjacent replies', () => {
  for (const link of [null, '', 'foreign', 12, [], 'a1']) {
    const rows = normalize({ messages: [row('u1', 'user', trace('A')), row('a1', 'assistant', undefined, link)] }).messages;
    assert.equal(rows[1].metadata.reply_to, null);
    assert.deepEqual(owners(rows), ['u1']);
  }
});
test('truly missing legacy link keeps adjacent user-to-ordinary-reply behavior', () => {
  const rows = normalize({ messages: [row('u1', 'user', trace('A')), row('a1', 'assistant')] }).messages;
  assert.equal(rows[1].metadata.reply_to, undefined);
  assert.deepEqual(owners(rows), ['a1']);
});
test('proactive matching-time record is independent and does not consume fallback', () => {
  const rows = normalize({ messages: [row('u1', 'user', trace('A')),
    row('p1', 'assistant', trace('P'), 'u1', 'proactive_screen'), row('a1', 'assistant')] }).messages;
  assert.deepEqual(owners(rows), ['p1', 'a1']);
  assert.equal(rows[1].metadata.reply_to, null);
});
test('completed and needs_user inherit the exact linked reply across other user', () => {
  for (const state of ['completed', 'needs_user']) {
    const rows = [row('u1', 'user', trace('A', state)), row('u2', 'user'), row('a1', 'assistant', undefined, 'u1')];
    assert.deepEqual(owners(rows), ['a1']);
  }
});
test('running failed interrupted user-only trace remains on its own request', () => {
  for (const state of ['running', 'failed', 'interrupted']) {
    assert.deepEqual(owners([row('u1', 'user', trace('A', state)), row('a1', 'assistant', undefined, 'u1')]), ['u1']);
  }
});
test('one request with two execution attempts preserves both records', () => {
  assert.deepEqual(owners([row('u1', 'user', trace('new', 'running', '2026-11-06T03:00:01+00:00')),
    row('a1', 'assistant', trace('old'), 'u1')]), ['u1', 'a1']);
});
test('an explicit reply is not hidden by a later unlinked standalone record', () => {
  assert.deepEqual(owners([row('u1', 'user', trace('A')), row('a1', 'assistant', trace('A'), 'u1'),
    row('a2', 'assistant', trace('separate'))]), ['a1', 'a2']);
});
test('ordinary user target validation accepts legacy IDs and rejects non-user source', () => {
  const invalid = normalize({ messages: [row('u1', 'user', trace('A'), missing, 'foreign'),
    row('a1', 'assistant', undefined, 'u1')] }).messages;
  assert.equal(invalid[1].metadata.reply_to, null);
  assert.deepEqual(owners(invalid), ['u1']);
  const valid = normalize({ messages: [row('non-uuid-legacy', 'user', trace('A')),
    row('a1', 'assistant', trace('A'), 'non-uuid-legacy')] }).messages;
  assert.equal(valid[1].metadata.reply_to, 'non-uuid-legacy');
  assert.deepEqual(owners(valid), ['a1']);
});
const name = process.argv[2];
assert.match(name, /^[a-z0-9-]+$/);
const report = { passed: tests.every(item => item.passed), tests: tests.length, cases: tests,
  app_sha256: createHash('sha256').update(source).digest('hex'),
  bridge_sha256: createHash('sha256').update(bridge).digest('hex'), real_model_calls: 0,
  scope: 'Actual production App callback extracted by TypeScript AST and actual bridge normalization. No browser, model, provider or worker.' };
writeFileSync(resolve('artifacts/user-testing-20261105/request-execution-identity/' + name + '.json'), JSON.stringify(report, null, 2), { flag: 'wx' });
console.log(JSON.stringify(report));
process.exitCode = report.passed ? 0 : 1;
