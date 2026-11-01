/** Read-only capture recovery. Retries observations only, never model calls/input. */
import { spawn } from 'node:child_process';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Jimp } from 'jimp';
import { BridgeError } from './bridge.mjs';

export const CAPTURE_ERRORS = new Set(['SCREENSHOT_FAILED', 'DESKTOP_LOCKED', 'DESKTOP_SESSION_UNAVAILABLE',
  'SCREENSHOT_DISPLAY_UNAVAILABLE', 'SCREENSHOT_BLACK_FRAME']);
const TERMINAL = new Set(['DESKTOP_LOCKED', 'DESKTOP_SESSION_UNAVAILABLE']);
const abortCheck = (signal) => { if (signal.aborted) throw new DOMException('Operation aborted', 'AbortError'); };
const failure = (exception) => new BridgeError(CAPTURE_ERRORS.has(exception?.code) ? exception.code : 'SCREENSHOT_FAILED');

/** A fixed helper receives no task, credentials, user text, title, or output path. */
export async function windowsCaptureHelper(mode, signal, executable, startProcess = spawn) {
  abortCheck(signal);
  if (!['probe', 'capture'].includes(mode) || typeof executable !== 'string' ||
      !/^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$/i.test(basename(executable)) ||
      !/^(?:[A-Za-z]:[\\/]|\/)/.test(executable)) throw new BridgeError('SCREENSHOT_FAILED');
  const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT|PYTHONPATH/i.test(key)));
  const output = await new Promise((resolve, reject) => {
    const child = startProcess(executable, ['-I', '-u', fileURLToPath(new URL('./windows-screen-capture.py', import.meta.url))],
      { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true, env: environment });
    const chunks = []; let size = 0; let settled = false; let aborted = false; let timedOut = false;
    const done = (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer); signal.removeEventListener('abort', stop);
      if (error) reject(error); else resolve(result);
    };
    const stop = () => { aborted = true; child.kill(); };
    const timer = setTimeout(() => { timedOut = true; child.kill(); }, 5000);
    signal.addEventListener('abort', stop, { once: true });
    child.stdout.on('data', (chunk) => {
      size += chunk.length;
      if (size > (mode === 'probe' ? 16384 : 96 * 1024 * 1024)) { child.kill(); done(new BridgeError('SCREENSHOT_FAILED')); }
      else chunks.push(chunk);
    });
    child.on('error', () => done(aborted ? new DOMException('Operation aborted', 'AbortError') : new BridgeError('SCREENSHOT_FAILED')));
    child.stdin.on('error', () => { if (!aborted) done(new BridgeError('SCREENSHOT_FAILED')); });
    child.on('close', (code) => {
      if (aborted) return done(new DOMException('Operation aborted', 'AbortError'));
      if (timedOut) return done(new BridgeError('SCREENSHOT_FAILED'));
      try {
        const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (result?.ok !== true) return done(failure(result));
        if (code !== 0) return done(new BridgeError('SCREENSHOT_FAILED'));
        done(null, result);
      } catch { done(new BridgeError('SCREENSHOT_FAILED')); }
    });
    if (signal.aborted) stop();
    else child.stdin.end(JSON.stringify({ mode, inputPid: process.pid }));
  });
  abortCheck(signal);
  validateMetadata(output);
  return output;
}

function validateMetadata(value) {
  const rect = (v) => Array.isArray(v) && v.length === 4 && v.every(Number.isSafeInteger) &&
    v[0] < v[2] && v[1] < v[3] && (v[2] - v[0]) * (v[3] - v[1]) <= 32_000_000;
  if (!rect(value?.bounds) || !Array.isArray(value.monitors) || !value.monitors.length ||
      value.monitors.length > 64 || !value.monitors.every(rect) ||
      !value.monitors.some((v) => JSON.stringify(v) === JSON.stringify(value.bounds)) ||
      typeof value.primary !== 'boolean') throw new BridgeError('SCREENSHOT_DISPLAY_UNAVAILABLE');
  if (value.inputBounds !== undefined && (!rect(value.inputBounds) || !Array.isArray(value.inputMonitors) ||
      !value.inputMonitors.length || !value.inputMonitors.every(rect) ||
      !value.inputMonitors.some((v) => JSON.stringify(v) === JSON.stringify(value.inputBounds)))) {
    throw new BridgeError('SCREENSHOT_DISPLAY_UNAVAILABLE');
  }
}

function sameDisplay(first, second) {
  return JSON.stringify(first.bounds) === JSON.stringify(second.bounds) &&
    JSON.stringify(first.monitors) === JSON.stringify(second.monitors) &&
    JSON.stringify(first.inputBounds ?? first.bounds) === JSON.stringify(second.inputBounds ?? second.bounds) &&
    JSON.stringify(first.inputMonitors ?? first.monitors) === JSON.stringify(second.inputMonitors ?? second.monitors);
}

async function validateFrame(output, metadata) {
  validateMetadata(metadata);
  if (typeof output?.base64 !== 'string' || output.base64.length > 96 * 1024 * 1024 ||
      !Number.isFinite(output.scaleFactor) || output.scaleFactor <= 0 || output.scaleFactor > 8) {
    throw new BridgeError('SCREENSHOT_FAILED');
  }
  let image;
  try { image = await Jimp.fromBuffer(Buffer.from(output.base64, 'base64')); }
  catch { throw new BridgeError('SCREENSHOT_FAILED'); }
  const { bitmap } = image;
  const [left, top, right, bottom] = metadata.bounds;
  const inputBounds = metadata.inputBounds ?? metadata.bounds;
  if (!metadata.primary || left !== 0 || top !== 0 || inputBounds[0] !== 0 || inputBounds[1] !== 0) {
    throw new BridgeError('SCREENSHOT_DISPLAY_UNAVAILABLE');
  }
  const inputWidth = inputBounds[2] - inputBounds[0], inputHeight = inputBounds[3] - inputBounds[1];
  const native = metadata.backend !== 'nutjs';
  const expectedWidth = native ? right - left : inputWidth, expectedHeight = native ? bottom - top : inputHeight;
  const scaleFactor = (right - left) / inputWidth;
  if (bitmap.width !== expectedWidth || bitmap.height !== expectedHeight ||
      Math.abs(output.scaleFactor - (native ? 1 : scaleFactor)) > .02) throw new BridgeError('SCREENSHOT_DISPLAY_UNAVAILABLE');
  let dark = 0;
  for (let y = 0; y < 32; y++) for (let x = 0; x < 48; x++) {
    const offset = (Math.min(bitmap.height - 1, Math.floor((y + .5) * bitmap.height / 32)) * bitmap.width +
      Math.min(bitmap.width - 1, Math.floor((x + .5) * bitmap.width / 48))) * 4;
    if (Math.max(bitmap.data[offset], bitmap.data[offset + 1], bitmap.data[offset + 2]) <= 12) dark++;
  }
  if (dark / (48 * 32) >= .985) throw new BridgeError('SCREENSHOT_BLACK_FRAME');
  // Win32 GDI reads physical pixels; NutJS cursor movement uses its parent
  // Node DPI domain. Keep model pixels and input coordinates in that same
  // domain, including the fractional ratios caused by integer rounding.
  let base64 = output.base64;
  if (native && (bitmap.width !== inputWidth || bitmap.height !== inputHeight)) {
    base64 = (await image.resize({ w: inputWidth, h: inputHeight }).getBuffer('image/png')).toString('base64');
  }
  return { base64, scaleFactor, capture: {
    bounds: metadata.bounds, monitors: metadata.monitors, primary: metadata.primary, backend: metadata.backend,
    inputBounds, inputMonitors: metadata.inputMonitors ?? metadata.monitors,
    dpiAwareness: metadata.dpiAwareness,
  } };
}

async function waitForCapture(signal) {
  await new Promise((resolve, reject) => {
    abortCheck(signal);
    const stop = () => { clearTimeout(timer); reject(new DOMException('Operation aborted', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', stop); resolve(); }, 150);
    signal.addEventListener('abort', stop, { once: true });
  });
}

/** Prefer a fresh GDI frame; NutJS is an independent, primary-monitor fallback. */
export async function captureWindowsScreen(signal, executable, sdkScreenshot, helper = windowsCaptureHelper) {
  let last = new BridgeError('SCREENSHOT_FAILED');
  for (let attempt = 0; attempt < 2; attempt++) {
    abortCheck(signal);
    try {
      const native = await helper('capture', signal, executable);
      return await validateFrame(native, native);
    } catch (exception) {
      if (exception?.name === 'AbortError' || TERMINAL.has(exception?.code)) throw exception;
      last = failure(exception);
    }
    try {
      // Never use NutJS on an inaccessible desktop or silently pair a secondary
      // monitor's coordinates with its primary-monitor bitmap.
      const before = await helper('probe', signal, executable);
      if (before.primary && before.bounds[0] === 0 && before.bounds[1] === 0) {
        const snapshot = await sdkScreenshot();
        abortCheck(signal);
        const after = await helper('probe', signal, executable);
        if (!sameDisplay(before, after)) throw new BridgeError('SCREENSHOT_DISPLAY_UNAVAILABLE');
        return await validateFrame(snapshot, { ...after, backend: 'nutjs' });
      }
    } catch (exception) {
      if (exception?.name === 'AbortError' || TERMINAL.has(exception?.code)) throw exception;
      last = failure(exception);
    }
    if (attempt === 0) await waitForCapture(signal);
  }
  throw last;
}

export async function assertCaptureDesktop(capture, signal, executable, helper = windowsCaptureHelper) {
  const current = await helper('probe', signal, executable);
  if (!sameDisplay(capture, current)) throw new BridgeError('SCREENSHOT_DISPLAY_UNAVAILABLE');
}
