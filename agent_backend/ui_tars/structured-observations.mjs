/** Host-only bounded Windows UIA facts for the independent result judge.
 *
 * Validation establishes provenance shape, not application-goal success. UI
 * names and values are untrusted reference data. No model text, refs, command
 * arguments, password values or invented action history is accepted here.
 */
export const STRUCTURED_OBSERVATION_LIMIT = 64 * 1024;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value) &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value));
const exact = (value, keys) => object(value) && Object.keys(value).length === keys.length &&
  Object.keys(value).every(key => keys.includes(key));
const integer = (value, min, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;
const text = (value, max, multiline = false) => typeof value === 'string' && value.length <= max &&
  !/[\ud800-\udfff]/u.test(value) &&
  !(multiline ? /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/ : /[\x00-\x1f\x7f]/).test(value);
const stamp = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value && Date.parse(value) <= Date.now() + 2000;
const fail = () => { throw new TypeError('INVALID_STRUCTURED_OBSERVATIONS'); };
const frameKeys = ['source', 'status', 'sequence', 'captured_at', 'first_sequence', 'first_captured_at', 'capture_count',
  'snapshot_id', 'window', 'controls', 'truncated', 'scope', 'offset', 'query', 'next_offset'];
const summaryKeys = ['total_captures', 'retained_states', 'retained_captures', 'omitted_captures', 'collapsed_repeats'];
const stateKeys = ['enabled', 'focused', 'checked', 'selected', 'read_only', 'expanded', 'value'];

export function emptyStructuredObservations() {
  return { frames: [], summary: { total_captures: 0, retained_states: 0, retained_captures: 0,
    omitted_captures: 0, collapsed_repeats: 0 } };
}

export function validateStructuredObservations(value) {
  if (value === undefined) return emptyStructuredObservations();
  if (!exact(value, ['frames', 'summary']) || !Array.isArray(value.frames) || value.frames.length > 8 ||
      !exact(value.summary, summaryKeys) || !summaryKeys.every(key => integer(value.summary[key], 0))) fail();
  let encoded;
  try { encoded = JSON.stringify(value); } catch { fail(); }
  if (Buffer.byteLength(encoded, 'utf8') > STRUCTURED_OBSERVATION_LIMIT) fail();
  const ids = new Set(); let priorSequence = 0; let priorTime = -Infinity; let captures = 0;
  for (const frame of value.frames) {
    if (!exact(frame, frameKeys) || frame.source !== 'windows_uia' || frame.status !== 'observed' ||
        !integer(frame.sequence, 1) || !integer(frame.first_sequence, 1, frame.sequence) || frame.first_sequence <= priorSequence ||
        !integer(frame.capture_count, 1, frame.sequence - frame.first_sequence + 1) ||
        !stamp(frame.captured_at) || !stamp(frame.first_captured_at) ||
        Date.parse(frame.first_captured_at) > Date.parse(frame.captured_at) || Date.parse(frame.first_captured_at) < priorTime ||
        typeof frame.snapshot_id !== 'string' || !/^[a-f0-9]{16}$/.test(frame.snapshot_id) || ids.has(frame.snapshot_id) ||
        !exact(frame.window, ['title', 'process_id']) || !text(frame.window.title, 240) || !integer(frame.window.process_id, 1, 2 ** 31 - 1) ||
        !Array.isArray(frame.controls) || frame.controls.length > 120 || typeof frame.truncated !== 'boolean' ||
        !['foreground_window_accessibility_only', 'selected_window_accessibility'].includes(frame.scope) ||
        !integer(frame.offset, 0, 1500) || !text(frame.query, 200) ||
        !(frame.next_offset === null || integer(frame.next_offset, 1, 1500))) fail();
    ids.add(frame.snapshot_id); priorSequence = frame.sequence; priorTime = Date.parse(frame.captured_at); captures += frame.capture_count;
    for (const control of frame.controls) {
      if (!exact(control, ['name', 'control_type', 'state', 'protected']) || !text(control.name, 240) ||
          !text(control.control_type, 80) || !/^ControlType\.[A-Za-z]{1,60}$/.test(control.control_type) ||
          !object(control.state) || Object.keys(control.state).some(key => !stateKeys.includes(key)) || typeof control.protected !== 'boolean') fail();
      if (control.protected && (control.name !== '[protected control]' || Object.keys(control.state).length !== 0)) fail();
      for (const [key, item] of Object.entries(control.state)) {
        if (key === 'value' ? !text(item, 1024, true) :
            !(typeof item === 'boolean' || ['checked', 'selected'].includes(key) && item === null)) fail();
      }
    }
  }
  const summary = value.summary;
  if (summary.retained_states !== value.frames.length || summary.retained_captures !== captures ||
      summary.total_captures < captures || summary.omitted_captures !== summary.total_captures - captures ||
      summary.collapsed_repeats !== captures - value.frames.length ||
      value.frames.length && summary.total_captures !== value.frames.at(-1).sequence) fail();
  // Return detached JSON data so later caller mutations cannot alter the
  // already checked evidence supplied to a pending independent judgement.
  return JSON.parse(encoded);
}

function comparableCoverage(frame) {
  return JSON.stringify([frame.window.process_id, frame.window.title, frame.scope, frame.offset, frame.query,
    frame.next_offset, frame.truncated]);
}
function observedState(frame) {
  return JSON.stringify(frame.controls.map(control => JSON.stringify([control.name, control.control_type,
    Object.fromEntries(Object.entries(control.state).sort(([a], [b]) => a.localeCompare(b))), control.protected])).sort());
}

/** Only an evidence-availability test, never a goal verification decision.
 * Repeated final state, capture count, clock, refs and snapshot UUID changes
 * cannot mint a temporal state transition. Different windows/queries do not
 * establish identity or imply that missing controls were removed.
 */
export function hasStructuredStateTransition(evidence) {
  const frames = validateStructuredObservations(evidence).frames;
  for (let index = 0; index < frames.length; index++) {
    for (let next = index + 1; next < frames.length; next++) {
      // Comparable metadata is enough to ask the independent judge about a
      // transition, never enough to prove a unique window/control identity.
      if (comparableCoverage(frames[index]) === comparableCoverage(frames[next]) &&
          observedState(frames[index]) !== observedState(frames[next])) return true;
    }
  }
  return false;
}
