/** Snapshots only files explicitly delivered by the native present tool. */
import { open } from 'node:fs/promises';
import { basename, isAbsolute } from 'node:path';
import { createHash } from 'node:crypto';

export const MAX_ARTIFACT_BYTES = 16 * 1024 * 1024;

export async function snapshotPresentedFile(file) {
  if (!file || typeof file.path !== 'string' || !isAbsolute(file.path)) return null;
  let handle;
  try {
    handle = await open(file.path, 'r');
    const before = await handle.stat({ bigint: true });
    if (!before.isFile() || before.size > BigInt(MAX_ARTIFACT_BYTES)) return null;
    const hash = createHash('sha256');
    const buffer = Buffer.allocUnsafe(65536);
    let size = 0;
    for (;;) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (!bytesRead) break;
      size += bytesRead;
      if (size > MAX_ARTIFACT_BYTES) return null;
      hash.update(buffer.subarray(0, bytesRead));
    }
    const after = await handle.stat({ bigint: true });
    if (before.size !== BigInt(size) || ['size', 'mtimeNs', 'ctimeNs', 'ino', 'dev']
      .some(key => before[key] !== after[key])) return null;
    return { path: file.path, name: basename(file.path), sha256: hash.digest('hex'), size_bytes: size };
  } catch {
    return null; // A disappearing or oversized file must never get a download grant.
  } finally {
    await handle?.close();
  }
}

export async function snapshotPresentedFiles(files) {
  const snapshots = [];
  for (const file of files) {
    const snapshot = await snapshotPresentedFile(file);
    // Keep an ungranted placeholder so the host can surface failed delivery.
    snapshots.push(snapshot ?? { path: file.path });
  }
  return snapshots;
}
