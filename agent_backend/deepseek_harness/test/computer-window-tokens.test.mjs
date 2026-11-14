import assert from 'node:assert/strict';
import test from 'node:test';
import { WindowTokens } from '../../../third_party/deepseek_harness/cua-windows/compat-channel.mjs';

test('published window tokens round-trip native handles without numeric precision loss', () => {
  const tokens = new WindowTokens();
  const ids = ['128', '9007199254740993', '9223372036854775807'];
  const result = tokens.response({ result: { windowId: ids[0], focusedWindowId: ids[1],
    windows: ids.map(windowId => ({ windowId })), tree: 'Text including windowId remains opaque.' } });
  for (let index = 0; index < ids.length; index++) {
    const token = result.result.windows[index].windowId;
    assert.ok(Number.isSafeInteger(token));
    assert.equal(tokens.request({ params: { windowId: token } }).params.windowId, ids[index]);
  }
  assert.equal(result.result.windowId, result.result.windows[0].windowId);
  assert.equal(result.result.focusedWindowId, result.result.windows[1].windowId);
  assert.throws(() => tokens.resolve(9007199254740992), /UNKNOWN/);
  assert.throws(() => tokens.resolve(0), /UNKNOWN/);
  const oldToken = result.result.windowId;
  const nextConnection = new WindowTokens();
  nextConnection.expose('456');
  assert.throws(() => nextConnection.resolve(oldToken), /UNKNOWN/);
  assert.throws(() => tokens.expose('1.5'), /INVALID/);
  assert.deepEqual(tokens.request({ params: { value: { windowId: 'user text' } } }),
    { params: { value: { windowId: 'user text' } } });
});
