import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const packageRoot = new URL('../../../third_party/deepseek_harness/cua-windows/', import.meta.url);

test('vendored Windows provider matches the fixed official source revision', () => {
  const manifest = JSON.parse(readFileSync(new URL('UPSTREAM.json', packageRoot), 'utf8'));
  const patches = JSON.parse(readFileSync(new URL('patches/MANIFEST.json', packageRoot), 'utf8')).patches;
  const hash = path => createHash('sha256').update(readFileSync(new URL(path, packageRoot))).digest('hex');
  assert.equal(manifest.revision, '39163dd30f703a79079e2d2446545cff3c453738');
  assert.equal(manifest.repository, 'https://github.com/deepseek-ai/deepseek-harness');
  assert.ok(Object.keys(manifest.source_sha256).length >= 50);
  for (const [path, expected] of Object.entries(manifest.source_sha256)) {
    const patch = patches.find(item => item.path === path);
    if (patch) {
      assert.equal(patch.original_sha256, expected, `Patch must preserve original identity: ${path}`);
      assert.equal(hash(patch.original), expected, `Original source changed: ${path}`);
      assert.equal(hash(patch.diff), patch.diff_sha256, `Recorded patch changed: ${path}`);
      assert.equal(hash(path), patch.patched_sha256, `Patched source changed: ${path}`);
    } else {
      assert.equal(hash(path), expected, `Official source changed: ${path}`);
    }
  }
  for (const [path, expected] of Object.entries({ ...manifest.local_files_sha256, ...manifest.generated_sha256 })) {
    assert.equal(hash(path), expected, `Recorded compatibility layer changed: ${path}`);
  }
});
