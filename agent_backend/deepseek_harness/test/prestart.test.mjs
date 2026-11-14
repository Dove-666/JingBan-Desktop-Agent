import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { HarnessRunner } from '../runner.mjs';

for (const mode of ['invalid-config', 'missing-helper', 'home-is-a-file', 'constructor-failure']) {
  test(`pre-start ${mode} proves no runtime started and releases the host cleanup gate`, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'dsh-prestart-'));
    const events = [];
    let constructed = 0;
    const runner = new HarnessRunner({ emit: event => events.push(event),
      computerOptions: { platform: 'win32', arch: 'x64', helperPath: join(directory, 'missing.exe') },
      Harness: class { constructor() { constructed++; throw new Error('fixture-constructor-failure'); } } });
    const frame = { type: 'run', id: 'prestart-fixture', task: 'Do not start a runtime.',
      config: { base_url: 'http://127.0.0.1:1/v1', model: 'fixture', api_key: 'owned-prestart-test' },
      options: { workspace: join(directory, 'workspace'), tools_enabled: true, computer_enabled: mode === 'missing-helper' },
      storage: { home: join(directory, 'home'), session_id: null }, context: {} };
    if (mode === 'invalid-config') delete frame.config.api_key;
    if (mode === 'home-is-a-file') await writeFile(frame.storage.home, 'owned fixture');
    await assert.rejects(runner.run(frame));
    assert.equal(constructed, mode === 'constructor-failure' ? 1 : 0);
    assert.equal(runner.harness, undefined);
    assert.deepEqual(events, [{ type: 'stopped', id: frame.id, computer_settled: true }]);
  });
}

test('a started runtime error still needs shutdown and normal exit proof', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-started-'));
  const events = [];
  const runner = new HarnessRunner({ emit: event => events.push(event), Harness: class {
    constructor() { this.client = { child: { exitCode: 1 }, start() {}, onRequest() {}, async request() { return {}; } }; }
    async start() { throw new Error('fixture-started-runtime-error'); }
    async close() { await this.client.request('shutdown', {}); }
  } });
  await assert.rejects(runner.run({ type: 'run', id: 'started-fixture', task: 'Fail after constructing a runtime.',
    config: { base_url: 'http://127.0.0.1:1/v1', model: 'fixture', api_key: 'owned-prestart-test' },
    options: { workspace: join(directory, 'workspace'), tools_enabled: false, computer_enabled: false },
    storage: { home: join(directory, 'home'), session_id: null }, context: {} }));
  assert.equal(events.find(event => event.type === 'stopped').computer_settled, false);
});
