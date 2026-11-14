import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { computerPatch, inspectComputerRuntime, computerHelperPath } from '../computer.mjs';

test('disabled computer access does not inspect or mount the provider', () => {
  assert.deepEqual(computerPatch({ enabled: false, resolvePackage() { throw new Error('must not load'); } }), []);
  assert.deepEqual(computerPatch({}), []);
});

test('missing helper fails before mounting desktop tools', () => {
  const helperPath = join(tmpdir(), 'jingban-missing-cua-helper.exe');
  assert.equal(inspectComputerRuntime({ platform: 'win32', helperPath, fileExists: () => false }).code,
    'COMPUTER_HELPER_MISSING');
  assert.throws(() => computerPatch({ enabled: true, platform: 'win32', helperPath, fileExists: () => false }),
    /COMPUTER_HELPER_MISSING/);
});

test('only an absolute packaged helper can be selected', () => {
  assert.throws(() => computerHelperPath({ helperPath: 'untrusted.exe' }), /COMPUTER_HELPER_PATH_INVALID/);
  assert.equal(inspectComputerRuntime({ platform: 'linux' }).code, 'COMPUTER_PLATFORM_UNSUPPORTED');
  assert.equal(inspectComputerRuntime({ platform: 'win32', architecture: 'ia32' }).code,
    'COMPUTER_ARCHITECTURE_UNSUPPORTED');
});

test('SDK patch mounts the official provider without leaking unrelated configuration', () => {
  const directory = mkdtempSync(join(tmpdir(), 'jingban-cua-install-'));
  try {
    const helperPath = join(directory, 'dsh-cua-windows.exe');
    writeFileSync(helperPath, 'fixture');
    const resolvePackage = name => `file:///packaged/node_modules/${name}/lib/index.js`;
    const options = { enabled: true, platform: 'win32', helperPath,
      resolvePackage, apiKey: 'private-fixture-key' };
    const rows = computerPatch(options);
    assert.deepEqual(rows, [{ insert: [
      { id: 'jingban-computer-use', name: resolvePackage('@deepseek-ai/dsh-computer-use') },
      { id: 'jingban-cua-windows', name: resolvePackage('@deepseek-ai/dsh-cua-windows'),
        config: { helperPath, agentPreset: false } },
    ] }]);
    assert.equal(JSON.stringify(rows).includes('private-fixture-key'), false);
    assert.equal(inspectComputerRuntime(options).tool, 'cua');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
