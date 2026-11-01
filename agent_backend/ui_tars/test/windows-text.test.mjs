/** Process-boundary tests inject a fake helper; no native input is generated. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { resolve } from 'node:path';
import { sendWindowsUnicodeText } from '../sdk-adapter.mjs';

const PYTHON = resolve('host-selected-runtime', 'python.exe');
function helper(result = { ok: true }, exitCode = 0, automatic = true) {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stdin = new EventEmitter();
  const calls = [];
  child.stdin.end = (value) => { child.input = value; if (automatic) queueMicrotask(() => { child.stdout.emit('data', Buffer.from(JSON.stringify(result))); child.emit('close', exitCode); }); };
  child.kill = () => { child.killed = true; };
  return { child, calls, spawn(...args) { calls.push(args); return child; } };
}

test('Unicode, emoji and exact whitespace go only through private helper stdin, with fixed arguments', async () => {
  const fake = helper(); const text = '  中文🙂 AURA\nsecond line 两行  ';
  await sendWindowsUnicodeText(text, new AbortController().signal, PYTHON, fake.spawn);
  assert.equal(fake.calls.length, 1);
  assert.equal(fake.calls[0][0], PYTHON);
  assert.equal(fake.calls[0][1][0], '-I'); assert.equal(fake.calls[0][1][1], '-u');
  assert.ok(fake.calls[0][1][2].endsWith('windows-text-input.py'));
  assert.ok(!JSON.stringify(fake.calls[0][1]).includes(text));
  assert.deepEqual(JSON.parse(fake.child.input), { text });
  assert.equal(fake.calls[0][2].windowsHide, true);
  assert.ok(!Object.hasOwn(fake.calls[0][2], 'shell'));
});

test('terminal UI-TARS submit convention converts to one newline and helper failure is never retried', async () => {
  const fake = helper({ ok: false }, 1);
  await assert.rejects(sendWindowsUnicodeText('hello\\n', new AbortController().signal, PYTHON, fake.spawn), { code: 'ACTION_FAILED' });
  assert.equal(fake.calls.length, 1);
  assert.deepEqual(JSON.parse(fake.child.input), { text: 'hello\n' });
});

test('cancellation kills the owned helper and waits for process close before completing', async () => {
  const fake = helper({}, 0, false); const controller = new AbortController();
  let settled = false;
  const pending = sendWindowsUnicodeText('abc', controller.signal, PYTHON, fake.spawn).finally(() => { settled = true; });
  controller.abort(); await Promise.resolve();
  assert.equal(fake.child.killed, true); assert.equal(settled, false);
  fake.child.emit('close', null);
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(fake.calls.length, 1);
});

test('invalid interpreter, invalid text and cancellation are rejected before starting a helper', async () => {
  const fake = helper(); const controller = new AbortController();
  for (const executable of ['python', 'cmd.exe', 'relative/python.exe']) {
    await assert.rejects(sendWindowsUnicodeText('abc', controller.signal, executable, fake.spawn), { code: 'ACTION_FAILED' });
  }
  await assert.rejects(sendWindowsUnicodeText('a\0b', controller.signal, PYTHON, fake.spawn), { code: 'ACTION_FAILED' });
  controller.abort();
  await assert.rejects(sendWindowsUnicodeText('abc', controller.signal, PYTHON, fake.spawn), { name: 'AbortError' });
  assert.equal(fake.calls.length, 0);
});
