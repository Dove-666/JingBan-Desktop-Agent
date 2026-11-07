/** Bounded, chronological screen evidence; no planner prose or invented states.
 *
 * Short tasks retain every real frame, so opening an app, entering its feed,
 * selecting the second item and liking it do not collapse into three images.
 * Long tasks retain initial anchors, before/after navigation pairs and the
 * newest frame. Sampling gaps remain explicit in the original sequence IDs.
 * Everything is request-local memory and is cleared when the task ends.
 */
const ACTIONS = new Set(['click', 'left_click', 'left_single', 'left_double', 'double_click',
  'right_click', 'right_single', 'middle_click', 'drag', 'left_click_drag', 'select', 'hover',
  'mouse_move', 'type', 'hotkey', 'scroll', 'wait']);
const STRONG_NAVIGATION = new Set(['scroll', 'drag', 'left_click_drag']);
const WEAK_NAVIGATION = new Set(['click', 'left_click', 'left_single', 'left_double', 'double_click',
  'right_click', 'right_single', 'hotkey', 'select']);

export class BoundedObservations {
  constructor({ maxFrames = 8, maxBytes = 24 * 1024 * 1024, clock = () => new Date().toISOString() } = {}) {
    if (!Number.isSafeInteger(maxFrames) || maxFrames < 3 || maxFrames > 8 ||
        !Number.isSafeInteger(maxBytes) || maxBytes <= 0 || maxBytes > 24 * 1024 * 1024 ||
        typeof clock !== 'function') throw new TypeError('Invalid observation budget');
    this.maxFrames = maxFrames;
    this.maxBytes = maxBytes;
    this.clock = clock;
    this.clear();
  }

  clear() {
    this.frames = [];
    this.sequence = 0;
    this.initial = [];
    this.firstNavigation = null;
    this.lastNavigation = null;
    this.pending = null;
    this.bytes = 0;
  }

  get latest() { return this.frames.at(-1) ?? null; }
  get size() { return this.frames.length; }

  restore(evidence) {
    // Only BridgeController's request-local memory supplies this value; the
    // JSONL input validator does not admit caller-authored screenshot history.
    const frames = evidence?.frames;
    if (!Array.isArray(frames) || frames.length > this.maxFrames) return;
    if (!frames.every(frame => typeof frame?.base64 === 'string' && frame.base64.length > 0 &&
        Number.isSafeInteger(frame.sequence) && frame.sequence > 0 &&
        typeof frame.captured_at === 'string' && Number.isFinite(Date.parse(frame.captured_at)))) return;
    this.frames = frames.map(frame => ({ ...frame }));
    this.sequence = Math.max(evidence?.summary?.total_observations ?? 0, ...frames.map(frame => frame.sequence), 0);
    this.initial = frames.slice(0, 3).map(frame => frame.sequence);
    this.trim();
  }

  actionSucceeded(action) {
    const safeAction = ACTIONS.has(action) ? action : 'unknown';
    // Only host-observed execution may attach an action to a real screenshot.
    // A caller cannot supply its own image identity or arbitrary metadata.
    this.pending = { action: safeAction, before: this.latest?.sequence ?? null,
      strength: STRONG_NAVIGATION.has(action) ? 2 : WEAK_NAVIGATION.has(action) ? 1 : 0 };
  }

  capture(base64) {
    if (typeof base64 !== 'string' || base64.length === 0 || base64.length > this.maxBytes) {
      throw new TypeError('Observation exceeds image budget');
    }
    const captured_at = this.clock();
    if (typeof captured_at !== 'string' || captured_at.length > 40 || !Number.isFinite(Date.parse(captured_at))) {
      throw new TypeError('Invalid observation time');
    }
    const sequence = ++this.sequence;
    const frame = { base64, sequence, captured_at, after_action: this.pending?.action ?? null };
    this.frames.push(frame);
    if (this.initial.length < 3) this.initial.push(sequence);
    if (this.pending?.strength && this.pending.before !== null) {
      const navigation = { strength: this.pending.strength, before: this.pending.before, after: sequence };
      if (!this.firstNavigation || navigation.strength > this.firstNavigation.strength) this.firstNavigation = navigation;
      if (!this.lastNavigation || navigation.strength >= this.lastNavigation.strength) this.lastNavigation = navigation;
    }
    this.pending = null;
    this.trim();
    return frame;
  }

  trim() {
    const candidates = new Map(this.frames.map(frame => [frame.sequence, frame]));
    const newest = this.latest?.sequence;
    const priorities = [newest, ...this.initial,
      this.firstNavigation?.before, this.firstNavigation?.after,
      this.lastNavigation?.before, this.lastNavigation?.after,
      ...this.frames.map(frame => frame.sequence).reverse()];
    const retained = new Map();
    let bytes = 0;
    for (const index of priorities) {
      const frame = candidates.get(index);
      if (!frame || retained.has(index) || retained.size >= this.maxFrames || bytes + frame.base64.length > this.maxBytes) continue;
      retained.set(index, frame);
      bytes += frame.base64.length;
    }
    this.frames = [...retained.values()].sort((a, b) => a.sequence - b.sequence);
    this.bytes = bytes;
  }

  observations() {
    return this.frames.map(frame => ({ ...frame }));
  }

  summary() {
    return { total_observations: this.sequence, retained_observations: this.size,
      omitted_observations: this.sequence - this.size, retained_base64_bytes: this.bytes,
      image_budget: this.maxFrames, byte_budget: this.maxBytes };
  }
}
