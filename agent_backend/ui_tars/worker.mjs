// stdout is an application protocol. Third-party SDK/native logs must never reach it.
const protocolWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = (_chunk, encoding, callback) => {
  const done = typeof encoding === 'function' ? encoding : callback;
  if (done) queueMicrotask(done);
  return true;
};
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'trace']) console[method] = () => {};

const { BridgeController } = await import('./bridge.mjs');
const { createSdkAgent } = await import('./sdk-adapter.mjs');
const emit = (event) => protocolWrite(JSON.stringify(event) + '\n');
const controller = new BridgeController({ emit, createAgent: createSdkAgent });
controller.ready();

// Private parent-child stdio, not a network listener. Keep the full admitted
// reference context rather than imposing a second model-context byte window.
let pending = [];
function handleLine(bytes) {
  const raw = bytes.toString('utf8').trim();
  if (!raw) return;
  try { controller.handle(JSON.parse(raw)); } catch {
    emit({ type: 'error', protocol: 1, id: null, code: 'INVALID_REQUEST', message: 'Invalid JSON request.' });
  }
}
process.stdin.on('data', (chunk) => {
  let start = 0; let newline;
  while ((newline = chunk.indexOf(10, start)) !== -1) {
    pending.push(chunk.subarray(start, newline));
    handleLine(Buffer.concat(pending));
    pending = [];
    start = newline + 1;
  }
  if (start < chunk.length) pending.push(chunk.subarray(start));
});

function shutdown() {
  controller.close();
  // Native actions are cooperative; the host may terminate its owned process tree after a grace period.
  if (controller.active) setTimeout(() => process.exit(0), 3000).unref();
}
process.stdin.on('end', () => {
  if (pending.length) { handleLine(Buffer.concat(pending)); pending = []; }
  shutdown();
});
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', () => {
  emit({ type: 'error', protocol: 1, id: controller.active?.id ?? null, code: 'WORKER_FAILED', message: 'GUI worker failed.' });
  shutdown(); process.exitCode = 1;
});
process.on('unhandledRejection', () => {
  emit({ type: 'error', protocol: 1, id: controller.active?.id ?? null, code: 'WORKER_FAILED', message: 'GUI worker failed.' });
  shutdown(); process.exitCode = 1;
});
