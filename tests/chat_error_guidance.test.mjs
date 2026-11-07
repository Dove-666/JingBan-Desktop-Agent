/** Production binding predicates and conversion reuse, without React or a model. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../frontend/chat-ui/src/App.tsx', import.meta.url), 'utf8');
const original = readFileSync(new URL('../artifacts/user-testing-20261105/error-guidance-dedup/before-source/frontend__chat-ui__src__App.tsx', import.meta.url), 'utf8');

function callbackText(text, name) {
  const tree = ts.createSourceFile('App.tsx', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name) callback = node.initializer.arguments[0];
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(callback, name);
  return ts.createPrinter().printNode(ts.EmitHint.Expression, callback, tree);
}

function actualCallback(name, parameters) {
  const compiled = ts.transpileModule(`const callback=${callbackText(source, name)}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  });
  return new Function(...parameters, `${compiled.outputText}\nreturn callback();`);
}

const tree = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declaration = tree.statements.find((node) => ts.isFunctionDeclaration(node) && node.name.text === 'guidanceShownGlobally');
assert.ok(declaration);
const compiled = ts.transpileModule(declaration.getText(tree), { compilerOptions: { target: ts.ScriptTarget.ES2022 } });
const shown = new Function(`${compiled.outputText}\nreturn guidanceShownGlobally;`)();
const binding = actualCallback('displayedError', ['state']);
const base = { ready: true, error: '请重新读取此会话。', errorAction: 'history', conversationId: 'current',
  errorConversationId: 'current', errorMessageId: 'u1', errorRetryStatus: '' };

assert.equal(shown(base.error, 'u1', 'current', binding(base)), true);
assert.equal(shown(base.error, 'u2', 'current', binding(base)), false);
assert.equal(shown(base.error, 'u1', 'other', binding(base)), false);
for (const change of [{ ready: false }, { error: '' }, { errorConversationId: 'other' }, { errorMessageId: '' }]) {
  assert.equal(binding({ ...base, ...change }), undefined);
}
const draining = { ...base, error: '独特的网络诊断。', errorAction: 'retry', errorRetryStatus: '上次请求仍在结束。' };
assert.equal(shown(draining.errorRetryStatus, 'u1', 'current', binding(draining)), true);
assert.equal(shown(draining.errorRetryStatus, 'u1', 'current', binding({ ...draining, errorAction: 'settings' })), false);
assert.equal(shown('请重新读取此会话。', 'u1', 'current', binding({ ...base, error: '独特诊断。请重新读取此会话。' })), false);
assert.equal(shown('', 'u1', 'current', binding(base)), false);
assert.equal(shown(undefined, 'u1', 'current', binding(base)), false);
assert.equal(shown(base.error, 'u1', 'current', undefined), false);
assert.equal(shown(base.error, 'u1', 'current', { ...binding(base), messageId: 'u2' }), false);

// Error-only HostContext changes must not become message conversion inputs.
assert.equal(callbackText(source, 'historyMessages'), callbackText(original, 'historyMessages'));
const convert = actualCallback('historyMessages', ['state', 'convertedMessageCache']);
const messages = Array.from({ length: 40 }, (_, index) => ({ id: `m${index}`, role: index % 2 ? 'assistant' : 'user',
  content: `Owned ${index}`, source: index % 2 ? 'assistant' : 'user', metadata: {} }));
const cache = { current: new WeakMap() };
const first = convert({ ...base, messages }, cache);
const second = convert({ ...base, error: '', messages }, cache);
assert.equal(first.length, 40);
for (let index = 0; index < first.length; index++) assert.equal(first[index], second[index]);

const report = { passed: true, scope: 'Production App function and useMemo callbacks extracted by TypeScript AST',
  binding_checks: 14, unchanged_history_conversion_callback: true, converted_message_references_preserved: 40,
  current_source_sha256: createHash('sha256').update(source).digest('hex'), real_model_calls: 0 };
writeFileSync(resolve(process.argv[2] || 'artifacts/user-testing-20261105/error-guidance-dedup/pure-web.json'), JSON.stringify(report, null, 2), { flag: 'wx' });
console.log(JSON.stringify(report));
