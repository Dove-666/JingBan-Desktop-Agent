import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { createHmac } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createNativeReceipt, verifyNativeReceipt } from '../native-receipt.mjs';
import { HarnessRunner } from '../runner.mjs';

async function receiptFixture() {
  const directory = await mkdtemp(join(tmpdir(), 'jingban-native-receipt-'));
  const expected = await createNativeReceipt(directory, 'session-native-fixture', 'run-fixture');
  const payload = { version: 1, nonce: expected.nonce, session_id: expected.session_id,
    run_id: expected.run_id, channels_started: 0, channels_closed: 0, disposed: true, ok: true };
  const store = async (value, key = expected.key) => writeFile(expected.path, JSON.stringify({ ...value,
    signature: createHmac('sha256', key).update(JSON.stringify(value)).digest('hex') }));
  return { expected, payload, store };
}

test('zero-channel proof requires the authentic current provider disposal', async () => {
  const { expected, payload, store } = await receiptFixture();
  assert.equal(await verifyNativeReceipt(expected), false);
  await store(payload);
  assert.equal(await verifyNativeReceipt(expected), true);
  await store({ ...payload, session_id: 'session-other' });
  assert.equal(await verifyNativeReceipt(expected), false);
  await store({ ...payload, run_id: 'other-run' });
  assert.equal(await verifyNativeReceipt(expected), false);
  await store(payload, 'wrong-key');
  assert.equal(await verifyNativeReceipt(expected), false);
});

test('failed or incomplete native channel closure never confirms settlement', async () => {
  const { expected, payload, store } = await receiptFixture();
  await store({ ...payload, channels_started: 1, channels_closed: 0, ok: false });
  assert.equal(await verifyNativeReceipt(expected), false);
  await store({ ...payload, channels_started: 1, channels_closed: 0 });
  assert.equal(await verifyNativeReceipt(expected), false);
  await store({ ...payload, channels_started: 1, channels_closed: 1 });
  assert.equal(await verifyNativeReceipt(expected), true);
});

test('SDK shutdown success with a missing native receipt still emits unconfirmed', async () => {
  const events = [];
  const runner = new HarnessRunner({ emit: event => events.push(event) });
  runner.request = { id: 'fixture', options: { computer_enabled: true } };
  runner.harness = { client: { child: { exitCode: 0 }, async request() { return {}; } },
    async close() { await this.client.request('shutdown', {}); } };
  await runner.cancel();
  assert.deepEqual(events, [{ type: 'stopped', id: 'fixture', computer_settled: false }]);
});
