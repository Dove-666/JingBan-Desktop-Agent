/** Real upstream-helper checks against an owned fixture, using its current RPC. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { createConnection } from 'node:net';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { resolve } from 'node:path';

assert.equal(process.platform, 'win32');
assert.equal(process.argv.slice(2).length, 3, 'Expected helper, owned fixture and evidence paths');
const [helperPath, fixturePath, evidencePath] = process.argv.slice(2).map(value => resolve(value));
const resources = [];
const checks = [];
let socket;
let failure;
function readLines(stream) {
  const lines = createInterface({ input: stream })[Symbol.asyncIterator]();
  return async () => {
    const item = await lines.next();
    assert.equal(item.done, false, 'Native process ended before its reply');
    return JSON.parse(item.value);
  };
}
function child(executable, args = []) {
  const child = spawn(executable, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const exited = once(child, 'exit');
  const resource = { child, exited, read: readLines(child.stdout), diagnostics: '' };
  child.stderr.on('data', chunk => { resource.diagnostics = (resource.diagnostics + chunk.toString()).slice(-4000); });
  resources.push(resource);
  return resource;
}
const deadline = setTimeout(() => {
  socket?.destroy();
  for (const resource of resources) resource.child.kill();
}, 120_000);
try {
  const fixture = child(fixturePath);
  const initial = await fixture.read();
  const fixtureState = async () => { fixture.child.stdin.write('inspect\n'); return fixture.read(); };
  const runtimeKey = createHash('sha256').update(await readFile(helperPath)).digest('hex');
  const helper = child(helperPath, ['--serve', '--instance-id', randomUUID().replaceAll('-', ''),
    '--runtime-key', runtimeKey, '--connect-timeout-ms', '15000', '--grace-ms', '3000']);
  const ready = await helper.read();
  assert.equal(ready.runtimeKey, runtimeKey);
  socket = createConnection(ready.endpoint);
  await once(socket, 'connect');
  const read = readLines(socket);
  const exchange = async message => { socket.write(JSON.stringify(message) + '\n'); return read(); };
  const hello = await exchange({ type: 'hello', protocol: 1, runtimeKey, config: {
    maxRequestBytes: 1_048_576, maxResponseBytes: 33_554_432, axTimeoutMs: 5000,
    textEventIntervalMs: 0, screenshotTimeoutMs: 10000, timeoutMs: 15000,
    graceMs: 3000, pollIntervalMs: 25, launchWaitMs: 1000, maxClickHoldMs: 1000,
  } });
  assert.equal(hello.instanceId, ready.instanceId);
  let sequence = 0;
  const request = async (method, params = {}) => {
    const result = await exchange({ id: String(++sequence), method, params });
    if (result.error) throw Object.assign(new Error(result.error.message), { code: result.error.code });
    return result.result;
  };
  const capabilities = await request('getCapabilities');
  assert.equal(capabilities.sessionLocked, false, 'An unlocked interactive session is required');
  checks.push({ name: 'native-handshake', passed: true });
  // Immediately follow each observed reply with another request. This regresses
  // the upstream response-flush race without hiding it behind client sleeps.
  for (let index = 0; index < 250; index++) {
    assert.equal((await request('getCapabilities')).sessionLocked, false);
  }
  checks.push({ name: '250-sequential-replies-without-delay', passed: true });
  const apps = await request('listApps');
  const entry = apps.find(app => app.id.toLowerCase() === fixturePath.toLowerCase());
  assert.ok(entry, 'The native application catalog must discover the owned executable');
  const app = await request('getApp', { selector: entry.id });
  const windows = await request('listWindows', { appHandle: app.appHandle });
  const window = windows.find(window => window.windowId === initial.state.window);
  assert.ok(window, 'Select only the exact owned fixture window');
  const target = { appHandle: app.appHandle, windowId: window.windowId };
  const observe = () => request('getState', { ...target, maxElements: 100, maxDepth: 20, details: true });
  let observation = await observe();
  const element = (observation, name) => {
    const row = observation.tree.split('\n').find(row => row.includes(name));
    assert.ok(row, `Owned fixture element missing: ${name}`);
    const match = row.match(/\[([^\]]+)\]/);
    assert.ok(match, 'The observation must contain an opaque element ID');
    return { ...target, snapshotId: observation.snapshotId, elementId: match[1] };
  };
  const editor = element(observation, 'CUA test text');
  checks.push({ name: 'application-window-accessibility', passed: true });
  const value = '鲸伴 DSH 测试 😀';
  await request('setValue', { ...editor, value });
  assert.equal((await fixtureState()).text, value);
  observation = await observe();
  await request('invoke', element(observation, 'Record click'));
  assert.equal((await fixtureState()).clicks, 1, 'The fixture independently confirms exactly one invocation');
  checks.push({ name: 'uia-unicode-and-invoke', passed: true });
  const capture = await request('screenshot', target);
  const image = Buffer.from(capture.data, 'base64');
  assert.ok(capture.pixelWidth > 0 && capture.pixelHeight > 0);
  assert.equal(image.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  checks.push({ name: 'owned-window-screenshot', passed: true, width: capture.pixelWidth,
    height: capture.pixelHeight, sha256: createHash('sha256').update(image).digest('hex') });
  await assert.rejects(request('setValue', { ...editor, value: 'must not be sent' }), { code: 'STALE_SNAPSHOT' });
  assert.equal((await fixtureState()).text, value);
  checks.push({ name: 'stale-snapshot-refused-without-input', passed: true });
  assert.deepEqual(await exchange({ type: 'close' }), { type: 'closed' });
  socket.end();
  socket = undefined;
  helper.child.stdin.end();
  assert.deepEqual(await helper.exited, [0, null]);
  fixture.child.stdin.write('close\n');
  assert.deepEqual(await fixture.exited, [0, null]);
  checks.push({ name: 'owned-process-cleanup', passed: true });
} catch (error) {
  failure = { code: error.code ?? error.name, message: error.message };
  process.exitCode = 1;
} finally {
  socket?.destroy();
  for (const resource of resources) {
    resource.child.stdin.end();
    if (resource.child.exitCode === null && resource.child.signalCode === null) resource.child.kill();
  }
  await Promise.allSettled(resources.map(resource => resource.exited));
  clearTimeout(deadline);
  const result = { passed: failure === undefined, boundary: 'official Windows helper and owned fixture; no model used',
    checks, ...(failure ? { failure, nativeDiagnostics: resources.map(resource => ({
      exitCode: resource.child.exitCode, signalCode: resource.child.signalCode, stderr: resource.diagnostics,
    })) } : {}) };
  await writeFile(evidencePath, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result));
}
