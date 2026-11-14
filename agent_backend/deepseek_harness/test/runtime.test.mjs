import test from 'node:test';
import assert from 'node:assert/strict';
import { runtimeSupported } from '../runtime.mjs';

test('the runtime rejects affected Node versions and accepts the fixed baseline', () => {
  for (const version of ['22.21.0', '24.9.0', '24.15.0', '24.19.9', '25.9.0', '26.6.9']) assert.equal(runtimeSupported(version), false);
  for (const version of ['24.20.0', '24.22.3', '26.7.0', '26.13.1', '27.0.0']) assert.equal(runtimeSupported(version), true);
});

test('an unknown runtime version cannot be reported as supported', () => {
  for (const version of ['', 'unknown', '24', '24.22', '24.22.3-preview', null]) assert.equal(runtimeSupported(version), false);
});
