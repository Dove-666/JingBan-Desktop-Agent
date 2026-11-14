/** Private proof that the native provider joined every opened CUA channel. */
import { randomBytes, randomUUID, createHmac, timingSafeEqual } from 'node:crypto';
import { mkdir, open } from 'node:fs/promises';
import { join } from 'node:path';

export async function createNativeReceipt(home, sessionId, runId) {
  const directory = join(home, 'native-receipts');
  await mkdir(directory, { recursive: true });
  const nonce = randomUUID().replaceAll('-', '');
  return { path: join(directory, `${nonce}.json`), nonce, session_id: sessionId,
    run_id: runId, key: randomBytes(32).toString('hex') };
}

export async function verifyNativeReceipt(expected) {
  if (!expected) return false;
  let handle;
  try {
    handle = await open(expected.path, 'r');
    const info = await handle.stat();
    if (!info.isFile() || info.size > 8192) return false;
    const receipt = JSON.parse(await handle.readFile('utf8'));
    const payload = { version: receipt.version, nonce: receipt.nonce,
      session_id: receipt.session_id, run_id: receipt.run_id,
      channels_started: receipt.channels_started, channels_closed: receipt.channels_closed,
      disposed: receipt.disposed, ok: receipt.ok };
    if (payload.version !== 1 || payload.nonce !== expected.nonce || payload.session_id !== expected.session_id ||
        payload.run_id !== expected.run_id || payload.disposed !== true || payload.ok !== true ||
        !Number.isSafeInteger(payload.channels_started) || payload.channels_started < 0 ||
        payload.channels_closed !== payload.channels_started || !/^[a-f0-9]{64}$/.test(receipt.signature ?? '')) return false;
    const signature = createHmac('sha256', expected.key).update(JSON.stringify(payload)).digest();
    return timingSafeEqual(signature, Buffer.from(receipt.signature, 'hex'));
  } catch { return false; }
  finally { await handle?.close(); }
}
