import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHmac } from 'node:crypto';
import { installNativeReceipt } from '../../../third_party/deepseek_harness/cua-windows/compat-channel.mjs';

async function setup() {
  const directory = await mkdtemp(join(tmpdir(), 'jingban-native-receipt-'));
  const token = { path: join(directory, 'receipt.json'), nonce: '12'.repeat(16),
    session_id: 'session-fixture', run_id: 'fixture', key: 'ab'.repeat(32) };
  const env = { JINGBAN_DSH_CUA_RECEIPT_TOKEN: JSON.stringify(token) };
  return { token, env };
}

test('native receipt waits for actual channel acknowledgement and binds its signature', async () => {
  const { token, env } = await setup();
  const closed = Promise.withResolvers();
  const runtime = installNativeReceipt({ connect: () => ({ waitForClose: () => closed.promise }),
    dispose: async () => {} }, env);
  assert.equal(env.JINGBAN_DSH_CUA_RECEIPT_TOKEN, undefined);
  runtime.connect();
  const disposing = runtime.dispose();
  await assert.rejects(stat(token.path), { code: 'ENOENT' });
  closed.resolve(); await disposing;
  const raw = await readFile(token.path, 'utf8'), receipt = JSON.parse(raw);
  const { signature, ...payload } = receipt;
  assert.equal(receipt.ok, true);
  assert.equal(receipt.channels_started, 1); assert.equal(receipt.channels_closed, 1);
  assert.equal(signature, createHmac('sha256', token.key).update(JSON.stringify(payload)).digest('hex'));
  assert.ok(!raw.includes(token.key));
});

test('native receipt preserves failed cleanup even when outer disposal swallows it', async () => {
  const { token, env } = await setup();
  const active = new Set();
  const runtime = installNativeReceipt({ connect: () => {
    const channel = { waitForClose: () => Promise.reject(new Error('NO_ACK')) };
    active.add(channel); queueMicrotask(() => active.delete(channel));
    return channel;
  }, dispose: async () => { active.clear(); } }, env);
  runtime.connect();
  await assert.rejects(runtime.dispose(), /NO_ACK/);
  const receipt = JSON.parse(await readFile(token.path, 'utf8'));
  assert.equal(receipt.ok, false);
  assert.equal(receipt.channels_started, 1); assert.equal(receipt.channels_closed, 0);
  const { signature, ...payload } = receipt;
  assert.equal(signature, createHmac('sha256', token.key).update(JSON.stringify(payload)).digest('hex'));
  assert.equal(active.size, 0);
});

test('zero-channel receipt is emitted only after provider disposal completes', async () => {
  const { token, env } = await setup();
  const disposed = Promise.withResolvers();
  const runtime = installNativeReceipt({ dispose: () => disposed.promise, connect: () => assert.fail() }, env);
  const disposing = runtime.dispose();
  await assert.rejects(stat(token.path), { code: 'ENOENT' });
  disposed.resolve(); await disposing;
  const receipt = JSON.parse(await readFile(token.path, 'utf8'));
  assert.equal(receipt.ok, true); assert.equal(receipt.disposed, true);
  assert.equal(receipt.channels_started, 0); assert.equal(receipt.channels_closed, 0);
});
