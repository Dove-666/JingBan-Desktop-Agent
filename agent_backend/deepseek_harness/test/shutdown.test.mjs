import test from 'node:test';
import assert from 'node:assert/strict';
import { HarnessRunner } from '../runner.mjs';

for (const scenario of [
  { name: 'resolved close without a shutdown acknowledgement', ack: false, exit: 0, settled: false },
  { name: 'acknowledged shutdown followed by forced process exit', ack: true, exit: 1, settled: false },
  { name: 'acknowledged disposal followed by normal process exit', ack: true, exit: 0, settled: true },
]) test(scenario.name, async () => {
  const events = [], requests = [];
  const runner = new HarnessRunner({ emit: event => events.push(event) });
  runner.request = { id: 'shutdown-fixture' };
  runner.harness = { client: { child: { exitCode: scenario.exit }, async request(method) {
    requests.push(method);
    if (!scenario.ack) throw new Error('RPC transport lost');
    return {};
  } }, async close() {
    try { await this.client.request('shutdown', {}); } catch { /* Official close also swallows this. */ }
  } };
  await runner.cancel();
  await runner.cancel();
  assert.deepEqual(requests, ['shutdown']);
  assert.deepEqual(events, [{ type: 'stopped', id: 'shutdown-fixture', computer_settled: scenario.settled }]);
  assert.equal(runner.shutdownConfirmed, scenario.settled);
});

test('a close that never sends shutdown cannot claim an acknowledgement', async () => {
  const runner = new HarnessRunner();
  runner.request = { id: 'missing-shutdown' };
  runner.harness = { client: { child: { exitCode: 0 }, async request() { return {}; } }, async close() {} };
  await runner.close();
  assert.equal(runner.shutdownConfirmed, false);
});
