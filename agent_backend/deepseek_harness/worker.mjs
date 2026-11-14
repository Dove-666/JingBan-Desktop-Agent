/** Private JSONL entry point. stdout contains protocol frames only. */
import { createInterface } from 'node:readline';
import { HarnessRunner, publicErrorCode } from './runner.mjs';
import { assertRuntimeSupported } from './runtime.mjs';

const emit = frame => process.stdout.write(`${JSON.stringify(frame)}\n`);
if (process.argv.includes('--doctor')) {
  try {
    assertRuntimeSupported();
    const sdk = await import('@deepseek-ai/dsh-sdk-client');
    const { readFile } = await import('node:fs/promises');
    const pkg = JSON.parse(await readFile(new URL(import.meta.resolve('@deepseek-ai/dsh-sdk-client/package.json')), 'utf8'));
    emit({ sdk: 'deepseek-harness', version: pkg.version, node_version: process.versions.node,
      loaded: typeof sdk.DeepSeekHarness === 'function' });
    process.exit(0);
  } catch (error) {
    emit({ sdk: 'deepseek-harness', loaded: false, node_version: process.versions.node,
      code: error.code === 'DSH_RUNTIME_UNSUPPORTED' ? error.code : 'DSH_RUNTIME_MISSING' });
    process.exit(1);
  }
}
const runner = new HarnessRunner({ emit });
let started = false;
let settled = false;
let requestId;
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
emit({ type: 'ready', protocol: 1, sdk: 'deepseek-harness' });
lines.on('line', line => {
  if (settled) return;
  let frame;
  try { if (line.length > 24 * 1024 * 1024) throw new Error(); frame = JSON.parse(line); }
  catch { emit({ type: 'error', id: requestId, code: 'INVALID_REQUEST' }); runner.cancel().catch(() => {}); return; }
  if (!started) {
    started = true;
    requestId = frame.id;
    runner.run(frame).catch(error => emit({ type: 'error', id: requestId,
      code: runner.cancelled ? 'CANCELLED' : publicErrorCode(error) })).finally(() => {
      settled = true;
      lines.close();
      process.stdin.destroy();
    });
  } else runner.control(frame).catch(error => {
    emit({ type: 'error', id: requestId, code: publicErrorCode(error) });
    runner.cancel().catch(() => {});
  });
});
lines.on('close', () => { if (!settled) runner.cancel().catch(() => {}); });
process.on('SIGTERM', () => runner.cancel().catch(() => {}));
process.on('SIGINT', () => runner.cancel().catch(() => {}));
