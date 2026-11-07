/** Actual production normalizer/card with a bounded memory-only React fixture. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../frontend/chat-ui/package.json', import.meta.url));
const ts = require('typescript');
function compile(path) {
  const result = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    reportDiagnostics: true,
  });
  assert.deepEqual(result.diagnostics, []);
  return result.outputText;
}
const bridge = { exports: {} };
new Function('require', 'module', 'exports', compile('../frontend/chat-ui/src/bridge.ts'))(require, bridge, bridge.exports);
const normalize = bridge.exports.normalizeAgentArtifacts;
const artifact = (name = 'Owned report.docx', letter = 'a') => ({ reference: 'artifact_' + letter.repeat(64), name,
  mime: 'application/octet-stream', bytes: 100, sha256: 'b'.repeat(64), verification_scope: 'format_and_readback' });
const source = compile('../frontend/chat-ui/src/components/AgentArtifactCards.tsx');
function fixture(artifacts = [artifact()], conversationId = 'owned-a') {
  const slots = [], requests = []; let index = 0;
  const state = { useState: initial => { const slot = index++; if (!(slot in slots)) slots[slot] = initial;
    return [slots[slot], value => { slots[slot] = value; }]; } };
  const jsx = (type, props) => ({ type, props });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', source)(name => name === 'react' ? state
    : name === 'react/jsx-runtime' ? { jsx, jsxs: jsx } : name === 'lucide-react' ? { Download: 'icon', FileText: 'icon' } : {}, module, module.exports);
  const render = () => { index = 0; return module.exports.AgentArtifactCards({ artifacts, conversationId, enabled: true,
    dispatch: (action, payload, callback) => { requests.push({ action, payload, callback }); return true; } }); };
  return { render, requests, slots };
}
const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node && typeof node === 'object'
  ? [node, ...nodes(node.props?.children)] : [];
const buttons = tree => nodes(tree).filter(node => node.type === 'button');
const outcomes = tree => nodes(tree).filter(node => node.props?.['data-testid'] === 'agent-artifact-result');
const text = node => Array.isArray(node) ? node.map(text).join('') : node && typeof node === 'object'
  ? text(node.props?.children) : typeof node === 'string' ? node : '';
const tests = [];
function test(name, action) { action(); tests.push(name); }
test('host-valid 238 codepoint 246 UTF16 filename survives normalization', () => {
  const name = '鲸'.repeat(225) + '🐳'.repeat(8) + '.docx';
  assert.equal(name.length, 246); assert.equal(Array.from(name).length, 238);
  assert.equal(normalize([artifact(name)])[0].name, name);
  assert.equal(normalize([artifact('鲸'.repeat(240))]).length, 1);
  assert.equal(normalize([artifact('🐳'.repeat(240))]).length, 1);
  assert.equal(normalize([artifact('鲸'.repeat(241))]).length, 0);
});
test('UTF16 prelimit rejects huge names before codepoint array allocation', () => {
  const original = Array.from;
  try { Array.from = () => { throw new Error('unbounded codepoint allocation'); };
    assert.deepEqual(normalize([artifact('x'.repeat(100000))]), []);
  } finally { Array.from = original; }
});
test('failed save has fixed visible error and can retry successfully', () => {
  const f = fixture(); buttons(f.render())[0].props.onClick();
  assert.equal(buttons(f.render())[0].props.disabled, true);
  f.requests[0].callback({ ok: false, status: 'failed', error: 'SYNTHETIC_SECRET_PROVIDER_TEXT' });
  const failed = f.render(); assert.equal(buttons(failed)[0].props.disabled, false);
  assert.equal(outcomes(failed)[0].props.role, 'alert');
  assert.equal(text(outcomes(failed)[0]), '保存失败，请检查保存位置或重新生成文件后再试。');
  assert.equal(text(failed).includes('SYNTHETIC_SECRET'), false);
  assert.equal(text(buttons(failed)[0]), '重试'); buttons(failed)[0].props.onClick();
  f.requests[1].callback({ ok: true, status: 'saved' });
  assert.equal(text(outcomes(f.render())[0]), '已保存');
  assert.deepEqual(f.requests[1].payload, { conversationId: 'owned-a', reference: artifact().reference });
});
test('explicit cancellation is silent and restores the button', () => {
  const f = fixture(); buttons(f.render())[0].props.onClick(); f.requests[0].callback({ ok: false, status: 'cancelled' });
  assert.equal(outcomes(f.render()).length, 0); assert.equal(buttons(f.render())[0].props.disabled, false);
});
test('legacy success is saved, legacy false is unknown, transport error is fixed failure', () => {
  for (const [result, expected] of [[{ ok: true }, '已保存'], [{ ok: false }, ''],
    [{ ok: false, error: 'SYNTHETIC_SECRET_TRANSPORT' }, '保存失败，请检查保存位置或重新生成文件后再试。']]) {
    const f = fixture(); buttons(f.render())[0].props.onClick(); f.requests[0].callback(result);
    assert.equal(text(outcomes(f.render())), expected); assert.equal(buttons(f.render())[0].props.disabled, false);
    assert.equal(text(f.render()).includes('SYNTHETIC_SECRET'), false);
  }
});
test('one pending save disables every card; only the last outcome is retained', () => {
  const f = fixture([artifact(), artifact('Other report.docx', 'c')]); buttons(f.render())[0].props.onClick();
  assert.equal(buttons(f.render()).every(button => button.props.disabled), true);
  f.requests[0].callback({ ok: false, status: 'failed' }); buttons(f.render())[1].props.onClick();
  f.requests[1].callback({ ok: true, status: 'saved' }); assert.equal(outcomes(f.render()).length, 1);
  assert.equal(f.slots.length, 2);
});
test('late old-CID callback cannot change the new keyed card instance', () => {
  const a = fixture(); buttons(a.render())[0].props.onClick();
  const b = fixture([artifact('New CID report.docx', 'c')], 'owned-b');
  a.requests[0].callback({ ok: false, status: 'failed' });
  assert.equal(buttons(b.render())[0].props.disabled, false); assert.equal(outcomes(b.render()).length, 0);
  assert.equal(b.requests.length, 0);
});
console.log(JSON.stringify({ scope: 'actual artifact normalizer and card, memory-only React state', passed: tests.length, tests,
  real_model_requests: 0, network: false, gui: false }));
