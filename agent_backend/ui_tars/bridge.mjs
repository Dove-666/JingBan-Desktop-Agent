/** UI-TARS protocol controller. This module never imports a native operator. */
import { sanitizeFeedback, sanitizeVerification, verificationReport } from './goal-verifier.mjs';
import { validateStructuredObservations } from './structured-observations.mjs';
export const PROTOCOL_VERSION = 1;
export const PROVIDERS = Object.freeze({
  'ui-tars-1.5': '1.5',
  'ui-tars-1.0': '1.0',
  'doubao-1.5-15b': 'doubao-1.5-15B',
  'doubao-1.5-20b': 'doubao-1.5-20B',
});

export class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

const error = (code, message) => new BridgeError(code, message);
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const validId = (id) => typeof id === 'string' && /^[\w.:\-]{1,128}$/.test(id);

function text(value, name, maxLength) {
  if (typeof value !== 'string' || !value.trim() || value.length > maxLength) {
    throw error('INVALID_REQUEST', `${name} must be a non-empty string (maximum ${maxLength} characters).`);
  }
  return value.trim();
}

function boundedInt(value, fallback, min, max, name) {
  const selected = value ?? fallback;
  if (!Number.isInteger(selected) || selected < min || selected > max) {
    throw error('INVALID_REQUEST', `${name} must be an integer between ${min} and ${max}.`);
  }
  return selected;
}

function taskTimeout(value) {
  // Zero means no timer for the whole task. Positive values are explicit
  // finite budgets; reject values Node would overflow into a 1 ms timer.
  const selected = boundedInt(value, 0, 0, 2_147_483_647, 'timeoutMs');
  if (selected !== 0 && selected < 1000) {
    throw error('INVALID_REQUEST', 'timeoutMs must be zero or at least 1000 milliseconds.');
  }
  return selected;
}

export function validateRun(message) {
  if (!isObject(message) || message.type !== 'run' || !validId(message.id)) {
    throw error('INVALID_REQUEST', 'A run requires a valid string id.');
  }
  const raw = message.config;
  if (!isObject(raw)) throw error('INVALID_REQUEST', 'A GUI model configuration is required.');
  const baseURL = text(raw.baseURL ?? raw.base_url, 'config.baseURL', 2048);
  let url;
  try { url = new URL(baseURL); } catch { throw error('INVALID_REQUEST', 'config.baseURL must be an HTTP(S) URL.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw error('INVALID_REQUEST', 'config.baseURL must be HTTP(S), without credentials, query, or fragment.');
  }
  const provider = raw.provider ?? 'ui-tars-1.5';
  if (!Object.hasOwn(PROVIDERS, provider)) throw error('INVALID_PROVIDER', 'Unsupported UI-TARS model format.');
  const coordinateProfile = raw.coordinateProfile ?? raw.coordinate_profile ?? 'native';
  if (!['native', 'pixel'].includes(coordinateProfile) || (coordinateProfile === 'pixel' && provider !== 'ui-tars-1.5')) {
    throw error('INVALID_REQUEST', 'The pixel coordinate profile requires UI-TARS 1.5 action format.');
  }
  const options = message.options ?? {};
  if (!isObject(options)) throw error('INVALID_REQUEST', 'options must be an object.');
  if (options.diagnostics !== undefined && typeof options.diagnostics !== 'boolean') throw error('INVALID_REQUEST', 'diagnostics must be a boolean.');
  if (options.verifyOnly !== undefined && typeof options.verifyOnly !== 'boolean') throw error('INVALID_REQUEST', 'verifyOnly must be a boolean.');
  if (options.pythonExecutable !== undefined && (typeof options.pythonExecutable !== 'string' ||
      !/^(?:[A-Za-z]:[\\/]|\/)/.test(options.pythonExecutable) || /[\r\n\0]/.test(options.pythonExecutable) ||
      !/(?:^|[\\/])python(?:\d+(?:\.\d+)*)?(?:\.exe)?$/i.test(options.pythonExecutable))) {
    throw error('INVALID_REQUEST', 'pythonExecutable must be a host-selected absolute Python interpreter path.');
  }
  const apiKey = raw.apiKey ?? raw.api_key ?? '';
  if (typeof apiKey !== 'string' || apiKey.length > 8192 || /[\r\n\0]/.test(apiKey)) {
    throw error('INVALID_REQUEST', 'config.apiKey must be a single-line string.');
  }
  text(message.task, 'task', 16000); // Validate without rewriting the authorized original.
  const subtask = message.subtask === undefined ? undefined : text(message.subtask, 'subtask', 8000);
  const referenceContext = message.reference_context ?? message.referenceContext ?? '';
  if (typeof referenceContext !== 'string') throw error('INVALID_REQUEST', 'reference_context must be text.');
  let structuredObservations;
  try { structuredObservations = validateStructuredObservations(message.structured_observations); }
  catch { throw error('INVALID_REQUEST', 'Host structured observations do not match the bounded evidence schema.'); }
  return {
    id: message.id,
    task: message.task,
    ...(subtask && { subtask }),
    referenceContext,
    structuredObservations,
    config: { baseURL: url.toString(), apiKey, model: text(raw.model, 'config.model', 256), provider, coordinateProfile },
    options: {
      // Zero is the wire sentinel for unlimited turns. Infinity stays inside
      // the SDK adapter and must never be serialized as JSON null.
      maxSteps: boundedInt(options.maxSteps ?? options.max_steps, 0, 0, Number.MAX_SAFE_INTEGER, 'maxSteps'),
      timeoutMs: taskTimeout(options.timeoutMs ?? options.timeout_ms),
      diagnostics: options.diagnostics ?? false,
      verifyOnly: options.verifyOnly ?? false,
      ...(options.pythonExecutable && { pythonExecutable: options.pythonExecutable }),
    },
  };
}

// SDK errors can contain the serialized OpenAI client. Never pass their text or stack out.
const SAFE_ERRORS = Object.freeze({
  SDK_UNAVAILABLE: 'UI-TARS SDK or the native Windows operator is unavailable. Run the local dependency probe.',
  UNSUPPORTED_PLATFORM: 'The Jingban GUI bridge currently supports Windows only.',
  STEP_LIMIT: 'The GUI task reached its configured step limit.',
  SCREENSHOT_FAILED: 'UI-TARS could not capture the desktop.',
  DESKTOP_LOCKED: 'The desktop is locked or a secure desktop is active. Unlock or finish the secure prompt locally.',
  DESKTOP_SESSION_UNAVAILABLE: 'The Windows desktop session is disconnected or unavailable.',
  SCREENSHOT_DISPLAY_UNAVAILABLE: 'The observed monitor is unavailable or changed before the operation.',
  SCREENSHOT_BLACK_FRAME: 'The capture returned an unreadable black frame. No model action was performed on that frame.',
  MODEL_FAILED: 'The GUI model request failed. Check the endpoint, key, model, and UI-TARS provider format.',
  CONTEXT_LENGTH_EXCEEDED: 'The complete GUI task context exceeds the model service window. Context was not silently truncated or retried.',
  ACTION_FAILED: 'The GUI action could not be executed.',
  UNSUPPORTED_ACTION: 'The model requested an unsupported GUI action.',
  NO_PROGRESS: 'The same action against an unchanged observation produced no new evidence. Re-observe and choose a different approach.',
  SDK_FAILED: 'The GUI task failed inside UI-TARS.',
});

export class BridgeController {
  constructor({ emit, createAgent, setTimer = setTimeout, clearTimer = clearTimeout }) {
    this.emit = emit;
    this.createAgent = createAgent;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.active = null;
    this.closed = false;
    this.sessionEvidence = null;
    this.sessionTask = null;
    this.sessionOwner = null;
  }

  send(event) { this.emit({ protocol: PROTOCOL_VERSION, ...event }); }

  ready(id) {
    this.send({ type: 'ready', ...(id && { id }), backend: 'ui-tars', sdkVersion: '1.2.3',
      providers: Object.keys(PROVIDERS), singleMonitor: true, lazy: true });
  }

  handle(message) {
    const id = validId(message?.id) ? message.id : null;
    if (this.closed) return;
    if (message?.type === 'ping') { this.ready(id); return; }
    if (message?.type === 'abort') {
      if (!id || this.active?.id !== id) {
        if (message.quiet !== true) this.send({ type: 'error', id, code: 'NOT_RUNNING', message: 'No matching GUI task is running.' });
      } else {
        this.abort('cancelled');
      }
      return;
    }
    if (message?.type !== 'run') {
      this.send({ type: 'error', id, code: 'INVALID_REQUEST', message: 'Supported commands are run, abort, and ping.' });
      return;
    }
    if (this.active) {
      this.send({ type: 'error', id, code: 'BUSY', message: 'Another GUI task is still running.' });
      return;
    }
    let run;
    try { run = validateRun(message); } catch (exception) {
      this.send({ type: 'error', id, code: exception.code ?? 'INVALID_REQUEST', message: exception.message });
      return;
    }
    const current = { id: run.id, controller: new AbortController(), handle: null, reason: null,
      steps: 0, status: null, summary: '', timer: null };
    this.active = current;
    if (run.options.timeoutMs > 0) {
      current.timer = this.setTimer(() => this.abort('timed_out'), run.options.timeoutMs);
    }
    this.send({ type: 'progress', id: run.id, stage: 'starting', step: 0 });
    // Deliberately do not await: stdin must continue reading abort while a run is active.
    void this.execute(run, current);
  }

  abort(reason) {
    const current = this.active;
    if (!current || current.reason) return;
    current.reason = reason;
    current.controller.abort();
    try { current.handle?.stop?.(); } catch { /* no raw SDK error logging */ }
    this.send({ type: 'progress', id: current.id, stage: 'stopping', step: current.steps });
  }

  async execute(run, current) {
    try {
      // Explicit host subcalls use <parent-id>:<action-digest>. A steering
      // correction changes authorization text, not the identity of actual past
      // observations. Legacy direct callers remain scoped to their task text.
      const owner = run.id.includes(':') ? run.id.slice(0, run.id.lastIndexOf(':')) : run.task;
      if (this.sessionOwner !== owner) this.sessionEvidence = null;
      this.sessionOwner = owner;
      this.sessionTask = run.task;
      current.handle = await this.createAgent({ ...run, priorEvidence: this.sessionEvidence, signal: current.controller.signal,
        onEvent: (event) => {
          if (this.active !== current || current.reason) return;
          current.steps = Math.max(current.steps, Number.isInteger(event.step) ? event.step : 0);
          // Explicit allowlist: no screenshots, task text, model reasoning, input contents, or config.
          if (event.stage === 'observing' || event.stage === 'acting' || event.stage === 'verifying') {
            this.send({ type: 'progress', id: run.id, stage: event.stage, step: current.steps,
              ...(['running', 'completed', 'failed'].includes(event.status) ? { status: event.status } : {}),
              ...(typeof event.action === 'string' && /^[a-z_]{1,40}$/.test(event.action) ? { action: event.action } : {}),
              ...(run.options.diagnostics && event.stage === 'acting' && isObject(event.point) &&
                [event.point.x, event.point.y].every((n) => typeof n === 'number' && Number.isFinite(n) && Math.abs(n) <= 100000) ?
                { point: { x: event.point.x, y: event.point.y } } : {}) });
          }
        } });
      if (current.reason) current.handle.stop?.();
      const result = await current.handle.run(run.task);
      if (result?.__evidence) this.sessionEvidence = result.__evidence;
      let status = current.reason ?? result?.status;
      if (!['completed', 'cancelled', 'timed_out', 'needs_user'].includes(status)) {
        throw error('SDK_FAILED', SAFE_ERRORS.SDK_FAILED);
      }
      const verification = !current.reason && result?.verification ? sanitizeVerification(result.verification) : verificationReport();
      let summary = result?.summary;
      if (status === 'completed' && verification.status !== 'verified') {
        status = 'needs_user';
        summary = '操作已停止；目前没有足够证据确认目标已完成。未自动重复操作。';
      }
      if (typeof summary !== 'string' || !summary.trim()) {
        summary = status === 'completed' ? 'GUI 操作已完成。' : status === 'needs_user' ? '此操作需要用户协助。' :
          status === 'timed_out' ? 'GUI 操作已超时停止。' : 'GUI 操作已停止。';
      }
      if (run.config.apiKey) summary = summary.split(run.config.apiKey).join('[redacted]');
      const effectState = ['not_dispatched', 'confirmed', 'uncertain'].includes(result?.effect_state) ? result.effect_state : undefined;
      const feedback = sanitizeFeedback(result?.feedback, run.config.apiKey);
      this.send({ type: 'final', id: run.id, status, summary, steps: current.steps, verification,
        ...(effectState && { effect_state: effectState }),
        ...(feedback && { feedback }),
        ...(Number.isSafeInteger(result?.actions_dispatched) && result.actions_dispatched >= 0 ? { actions_dispatched: result.actions_dispatched } : {}) });
    } catch (exception) {
      if (current.reason) {
        this.send({ type: 'final', id: run.id, status: current.reason,
          summary: current.reason === 'timed_out' ? 'GUI 操作已超时停止。' : 'GUI 操作已停止。', steps: current.steps });
      } else {
        const code = Object.hasOwn(SAFE_ERRORS, exception?.code) ? exception.code : 'SDK_FAILED';
        this.send({ type: 'error', id: run.id, code, message: SAFE_ERRORS[code], steps: current.steps,
          ...(['not_dispatched', 'confirmed', 'uncertain'].includes(exception?.effect_state) ? { effect_state: exception.effect_state } : {}),
          ...(Number.isSafeInteger(exception?.actions_dispatched) && exception.actions_dispatched >= 0 ? { actions_dispatched: exception.actions_dispatched } : {}) });
      }
    } finally {
      if (current.timer !== null) this.clearTimer(current.timer);
      // Credentials exist only while this request is alive, and are never persisted.
      run.config.apiKey = '';
      if (this.active === current) this.active = null;
    }
  }

  close() { this.closed = true; this.sessionEvidence = null; this.sessionTask = null; this.sessionOwner = null; this.abort('cancelled'); }
}
