// Tests of transport/grounding use an explicit isolated verifier. The verifier
// integration itself is exercised through real localhost HTTP in goal-verifier.test.
import assert from 'node:assert/strict';
import { verificationReport } from '../goal-verifier.mjs';
export async function fixtureVerified({ observations, signal }) {
  assert.equal(signal.aborted, false);
  assert.ok(observations.length > 0);
  assert.ok(observations.at(-1).base64);
  return verificationReport('verified', 'GOAL_STATE_CONFIRMED');
}
