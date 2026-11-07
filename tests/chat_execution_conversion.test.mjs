/** Actual App callback outputs and reference reuse across a single trace row. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const current = readFileSync(new URL('../frontend/chat-ui/src/App.tsx', import.meta.url), 'utf8');
const previous = readFileSync(new URL('../artifacts/user-testing-20261105/execution-incremental/before-source/frontend__chat-ui__src__App.tsx', import.meta.url), 'utf8');
function actualCallback(source, name, parameters) {
  const tree = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name) callback = node.initializer.arguments[0];
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(callback, name);
  const text = ts.createPrinter().printNode(ts.EmitHint.Expression, callback, tree);
  const compiled = ts.transpileModule('const callback=' + text, { compilerOptions: { target: ts.ScriptTarget.ES2022 } });
  return new Function(...parameters, compiled.outputText + '\nreturn callback();');
}
const before = actualCallback(previous, 'historyMessages', ['state']);
const after = actualCallback(current, 'historyMessages', ['state', 'convertedMessageCache']);
const oldFind = actualCallback(previous, 'findIds', ['state', 'findOpen', 'findQuery']);
const newFind = actualCallback(current, 'findIds', ['state', 'findOpen', 'findQuery', 'EMPTY_FIND_RESULTS']);
const trace = (state = 'running', id = 'attempt1') => ({ version: 1, state, started_at: id, duration_ms: 100, events: [] });
const message = (id, role = 'user', execution, extra = {}) => ({ id, role, content: 'Owned ' + id,
  source: role, metadata: { ...(execution ? { execution } : {}) }, ...extra });
const fixtures = [
  [message('u1'), message('a1', 'assistant')],
  [message('u1', 'user', trace())],
  [message('u1', 'user', trace('completed')), message('a1', 'assistant')],
  [message('u1', 'user', trace('needs_user')), message('a1', 'assistant')],
  [message('u1', 'user', trace('failed')), message('a1', 'assistant')],
  [message('u1', 'user', trace('completed')), message('u2'), message('a2', 'assistant')],
  [message('u1', 'user', trace('completed')), message('a1', 'assistant', trace('completed')),
    message('u2', 'user', trace('running', 'attempt2'))],
  [message('u1', 'user', trace(), { metadata: { execution: trace(), image_id: 'a'.repeat(64), image_revision: 3,
    selection_context: { text: 'Owned selection', title: 'Owned source', url: '' } }, retryAvailable: false, retryStatus: 'Owned' })],
  [message('u1', 'user', trace('completed')), message('p1', 'assistant', undefined, { source: 'proactive_screen' }),
    message('a1', 'assistant'), message('ignored', 'system')],
];
for (const rows of fixtures) {
  const state = { messages: rows };
  assert.deepEqual(after(state, { current: new WeakMap() }), before(state));
}
const count = 2000;
let rows = Array.from({ length: count }, (_, index) => message(String(index), index % 2 ? 'assistant' : 'user'));
rows[count - 1] = message(String(count - 1), 'user', trace());
const cache = { current: new WeakMap() };
let converted = after({ messages: rows }, cache);
let newBefore = 0, newAfter = 0;
for (let index = 0; index < 20; index++) {
  const prior = converted;
  rows = rows.slice();
  rows[count - 1] = { ...rows[count - 1], metadata: { execution: { ...trace(), duration_ms: 200 + index,
    events: [{ id: 'phase', kind: 'progress', status: 'running', label: 'Owned update ' + index, elapsed_ms: index }] } } };
  converted = after({ messages: rows }, cache);
  const oldOutput = before({ messages: rows });
  assert.deepEqual(converted, oldOutput);
  for (let item = 0; item < count - 1; item++) assert.equal(converted[item], prior[item]);
  assert.notEqual(converted[count - 1], prior[count - 1]);
  newAfter += converted.filter((value, item) => value !== prior[item]).length;
  newBefore += oldOutput.filter((value, item) => value !== prior[item]).length;
}
const empty = [], state = { messages: rows };
assert.notEqual(oldFind(state, false, ''), oldFind(state, false, ''));
assert.equal(newFind(state, false, '', empty), newFind({ messages: rows.slice() }, false, '', empty));
assert.deepEqual(newFind(state, true, 'Owned 1999', empty), oldFind(state, true, 'Owned 1999'));
const report = { scope: 'Production App callbacks extracted by TypeScript AST, no React renderer',
  semantic_fixtures: fixtures.length, trace_rounds: 20, messages: count,
  old_converted_references_recreated: newBefore, new_converted_references_recreated: newAfter,
  unchanged_rows_preserved_each_round: count - 1, empty_search_results_stable: true,
  previous_source_sha256: createHash('sha256').update(previous).digest('hex'),
  current_source_sha256: createHash('sha256').update(current).digest('hex'), real_model_calls: 0, passed: true };
writeFileSync(resolve('artifacts/user-testing-20261105/execution-incremental/conversion.json'), JSON.stringify(report, null, 2), { flag: 'wx' });
console.log(JSON.stringify(report));
