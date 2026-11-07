/** Read-only isolated cost of the exact production useMemo callback; no UI/network/build. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { performance } from 'node:perf_hooks';
import { resolve } from 'node:path';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
const root = resolve(new URL('..', import.meta.url).pathname.replace(/^\/(\w:)/, '$1'));
const path = resolve(root, 'frontend/chat-ui/src/App.tsx');
const compareInstalled = process.argv.includes('--compare-installed');
const compareSlice = compareInstalled || process.argv.includes('--compare-slice');
const output = resolve(root, process.argv.includes('--verify-final') ? 'artifacts/task-audit/history-slice-installed-final-20261106'
  : compareInstalled ? 'artifacts/task-audit/history-slice-installed-20261106'
  : compareSlice ? 'artifacts/task-audit/history-slice-candidate-20261106'
  : 'artifacts/task-audit/actual-history-conversion-20261106');
mkdirSync(output, { recursive: false });
const sha = value => createHash('sha256').update(value).digest('hex');
function extract(text) {
  const file = ts.createSourceFile(path, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(file.parseDiagnostics.length, 0);
  let found;
  function walk(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(file) === 'historyMessages') {
      const call = node.initializer;
      assert(ts.isCallExpression(call) && call.expression.getText(file) === 'useMemo');
      assert(ts.isArrowFunction(call.arguments[0]));
      assert.equal(call.arguments[1].getText(file), '[state.messages]');
      found = { text: call.arguments[0].getText(file), line: file.getLineAndCharacterOfPosition(call.arguments[0].getStart(file)).line + 1 };
    }
    ts.forEachChild(node, walk);
  }
  walk(file); assert(found); return found;
}
const before = readFileSync(path, 'utf8'), extracted = extract(before);
writeFileSync(resolve(output, 'actual-callback.ts.txt'), extracted.text);
// Compile only the extracted source fragment in memory; do not compile or write the project/index.
const compiled = ts.transpileModule(`const convert = ${extracted.text};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }, reportDiagnostics: true,
});
assert.equal(compiled.diagnostics.length, 0);
const convert = new Function('state', `${compiled.outputText}\nreturn convert();`);
if (compareSlice) {
  const oldText = compareInstalled
    ? extract(readFileSync(resolve(root, 'artifacts/task-audit/history-slice-optimization-20261106/App.tsx.before'), 'utf8')).text : extracted.text;
  const marker = 'for (const next of messages.slice(index + 1)) {';
  assert.equal(oldText.split(marker).length, 2);
  const candidateText = oldText.replace(marker,
    'for (let nextIndex = index + 1; nextIndex < messages.length; nextIndex++) {\n        const next = messages[nextIndex];');
  const candidate = ts.transpileModule(`const convert = ${candidateText};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }, reportDiagnostics: true,
  });
  assert.equal(candidate.diagnostics.length, 0);
  const changed = compareInstalled ? convert : new Function('state', `${candidate.outputText}\nreturn convert();`);
  const originalCode = ts.transpileModule(`const convert = ${oldText};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  });
  const baseline = compareInstalled ? new Function('state', `${originalCode.outputText}\nreturn convert();`) : convert;
  if (compareInstalled) assert.equal(extracted.text, candidateText);
  const trace = (state, key = 'one') => ({ state, started_at: `owned-${key}`, steps: [], elapsed_ms: 10 });
  const user = (id, execution) => ({ id, role: 'user', source: 'user', content: '合成问题', metadata: { execution } });
  const answer = (id, execution, source = 'assistant') => ({ id, role: 'assistant', source, content: '合成回答', metadata: { execution } });
  const vectors = [
    ['empty', []], ['last-user', [user('u', trace('completed'))]],
    ['completed', [user('u', trace('completed')), answer('a')]],
    ['needs-user', [user('u', trace('needs_user')), answer('a')]],
    ['running', [user('u', trace('running')), answer('a')]],
    ['next-user-stops', [user('u', trace('completed')), user('u2'), answer('a')]],
    ['nonassistant-source', [user('u', trace('completed')), answer('s', undefined, 'screen'), answer('a')]],
    ['explicit-assistant-trace', [user('u', trace('completed')), answer('a', trace('completed', 'two'))]],
    ['shared-trace-owner', [user('u', trace('completed')), answer('a', trace('completed'))]],
  ];
  const rows = [];
  for (const [name, messages] of vectors) {
    const input = structuredClone(messages), state = { messages };
    const oldOutput = baseline(state), newOutput = changed(state);
    assert.deepStrictEqual(newOutput, oldOutput, name);
    assert.deepStrictEqual(messages, input, name);
    rows.push({ name, output_identical: true, input_unchanged: true, output: newOutput });
  }
  const allocations = [];
  const countSlices = (callback, state) => {
    const slice = Array.prototype.slice; let calls = 0, slots = 0;
    try { Array.prototype.slice = function(start) {
      calls++; slots += Math.max(0, this.length - start); return slice.call(this, start);
    }; callback(state); } finally { Array.prototype.slice = slice; }
    return { calls, copied_reference_slots: slots };
  };
  for (const count of [2000, 5000]) {
    const messages = Array.from({ length: count }, (_, i) => i % 2 === 0 ? user(`u${i}`, trace('completed', String(i))) : answer(`a${i}`));
    const state = { messages }, input = structuredClone(messages);
    assert.deepStrictEqual(changed(state), baseline(state));
    assert.deepStrictEqual(messages, input);
    const old = countSlices(baseline, state), installed = countSlices(changed, state);
    assert.equal(old.calls, count / 2); assert.equal(old.copied_reference_slots, count * count / 4);
    assert.deepStrictEqual(installed, { calls: 0, copied_reference_slots: 0 });
    allocations.push({ count, old, candidate_or_installed: installed, output_identical: true, input_unchanged: true });
  }
  writeFileSync(resolve(output, 'candidate-callback.ts.txt'), candidateText);
  const report = { real_model_requests: 0, production_changed: false, project_build: false,
    source_before_sha256: sha(before), source_after_sha256: sha(readFileSync(path, 'utf8')),
    original_callback_sha256: sha(oldText), candidate_callback_sha256: sha(candidateText),
    installed_actual_callback: compareInstalled, allocations, rows };
  assert.equal(report.source_before_sha256, report.source_after_sha256);
  writeFileSync(resolve(output, 'verification.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: rows.length, source_unchanged: true, real_model_requests: 0 }));
  process.exit(0);
}
const cases = [];
for (const count of [2000, 5000]) for (const shape of ['plain', 'mixed_completed', 'all_completed_pairs']) {
  const messages = Array.from({ length: count }, (_, index) => {
    const user = index % 2 === 0;
    const traced = user && shape !== 'plain' && (shape === 'all_completed_pairs' || index % 10 === 0);
    return { id: `owned-${index}`, role: user ? 'user' : 'assistant', source: user ? 'user' : 'assistant',
      content: (`合成记录${index}。` + '仅用于生产函数离线审查。'.repeat(25)).slice(0, 200),
      metadata: traced ? { execution: { state: 'completed', started_at: `owned-task-${index}`, elapsed_ms: 10, steps: [] } } : {} };
  });
  const state = { messages }, original = sha(JSON.stringify(messages));
  for (let i = 0; i < 5; i++) convert(state);
  const elapsed = [], cpu = process.cpuUsage(); let result;
  for (let i = 0; i < 30; i++) { const start = performance.now(); result = convert(state); elapsed.push(performance.now() - start); }
  const used = process.cpuUsage(cpu), sorted = elapsed.toSorted((a, b) => a - b);
  assert.equal(result.length, count);
  const expectedTraces = messages.filter(message => message.metadata.execution).length;
  assert.equal(result.filter(message => message.metadata.custom.execution).length, expectedTraces);
  assert.equal(sha(JSON.stringify(messages)), original);
  const originalSlice = Array.prototype.slice; let calls = 0, elements = 0;
  try {
    Array.prototype.slice = function(start = 0, end = this.length) {
      calls++; const length = this.length;
      const a = start < 0 ? Math.max(0, length + start) : Math.min(length, start);
      const b = end < 0 ? Math.max(0, length + end) : Math.min(length, end);
      elements += Math.max(0, b - a); return originalSlice.call(this, start, end);
    };
    convert(state);
  } finally { Array.prototype.slice = originalSlice; }
  cases.push({ count, shape, completed_traces: expectedTraces, iterations: elapsed.length,
    wall_median_ms: sorted[Math.floor(sorted.length / 2)], wall_p95_ms: sorted[Math.ceil(sorted.length * .95) - 1],
    wall_max_ms: Math.max(...elapsed), cpu_ms_per_call: (used.user + used.system) / 1000 / elapsed.length,
    slice_calls: calls, copied_array_reference_slots: elements, input_unchanged: true });
}
const after = readFileSync(path, 'utf8'), afterCallback = extract(after);
const report = { real_model_requests: 0, gui: false, network: false, project_build: false,
  source_path: 'frontend/chat-ui/src/App.tsx', source_line: extracted.line,
  file_before_sha256: sha(before), file_after_sha256: sha(after), file_unchanged: before === after,
  actual_callback_sha256: sha(extracted.text), callback_unchanged: extracted.text === afterCallback.text, cases,
  scope: 'Exact production historyMessages useMemo callback extracted via AST and type-erased in memory; Node isolated execution, not Qt/WebEngine visible latency or FPS.',
};
assert(report.callback_unchanged);
writeFileSync(resolve(output, 'verification.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report));
