/** Detect an identical action against an unchanged observation, not step count. */
import { createHash } from 'node:crypto';

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

export class ProgressMonitor {
  constructor({ repeatedActions = 3 } = {}) {
    if (!Number.isSafeInteger(repeatedActions) || repeatedActions < 2) throw new TypeError('Invalid repetition threshold');
    this.threshold = repeatedActions; this.clear();
  }
  clear() { this.frame = null; this.last = null; this.repeats = 0; }
  observe(base64) { this.frame = createHash('sha256').update(base64).digest('hex'); }
  before(prediction) {
    const action = prediction?.action_type;
    if (['finished', 'call_user', 'error_env', 'wait', 'user_stop'].includes(action)) return { stalled: false };
    const key = createHash('sha256').update(JSON.stringify([this.frame, action, canonical(prediction?.action_inputs ?? {})])).digest('hex');
    this.repeats = key === this.last ? this.repeats + 1 : 1;
    this.last = key;
    return { stalled: this.repeats >= this.threshold, repeats: this.repeats };
  }
}
