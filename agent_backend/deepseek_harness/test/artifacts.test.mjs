import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, open, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { MAX_ARTIFACT_BYTES, snapshotPresentedFile, snapshotPresentedFiles } from '../artifacts.mjs';

test('presented bytes are fingerprinted exactly, including an empty file', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'jingban-dsh-artifacts-'));
  const file = join(directory, 'empty.txt');
  await writeFile(file, '');
  assert.deepEqual(await snapshotPresentedFile({ path: file }), {
    path: file, name: 'empty.txt', size_bytes: 0, sha256: createHash('sha256').digest('hex'),
  });
});

test('unavailable, directory and oversized deliveries retain no immutable grant', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'jingban-dsh-artifacts-'));
  const file = join(directory, 'oversized.bin');
  const handle = await open(file, 'w');
  try { await handle.truncate(MAX_ARTIFACT_BYTES + 1); } finally { await handle.close(); }
  const rows = [{ path: file }, { path: directory }, { path: join(directory, 'missing.txt') }];
  assert.deepEqual(await snapshotPresentedFiles(rows), rows);
});
