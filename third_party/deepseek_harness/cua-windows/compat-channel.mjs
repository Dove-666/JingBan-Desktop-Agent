/** Bridge published CUA numeric window tokens to upstream Windows opaque strings. */
import { Transform } from 'node:stream';
import { StringDecoder } from 'node:string_decoder';
import { writeFile, rename } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { randomInt, createHmac } from 'node:crypto';

export class WindowTokens {
  constructor() { this.native = new Map(); this.public = new Map(); }
  expose(value) {
    if (value == null) return value;
    if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) throw new Error('NATIVE_WINDOW_ID_INVALID');
    // Never parse a native handle as a JS number: nint may exceed its precision.
    if (!this.public.has(value)) {
      if (this.public.size >= 0xfffffffe) throw new Error('NATIVE_WINDOW_TOKEN_LIMIT');
      let token;
      do { token = randomInt(1, 0x100000000); } while (this.native.has(token));
      this.public.set(value, token); this.native.set(token, value);
    }
    return this.public.get(value);
  }
  resolve(value) {
    if (!Number.isSafeInteger(value) || value <= 0 || !this.native.has(value)) throw new Error('NATIVE_WINDOW_TOKEN_UNKNOWN');
    return this.native.get(value);
  }
  request(frame) {
    if (frame.params && Object.hasOwn(frame.params, 'windowId')) frame.params.windowId = this.resolve(frame.params.windowId);
    return frame;
  }
  response(frame) {
    const expose = result => {
      if (!result || typeof result !== 'object') return;
      for (const key of ['windowId', 'focusedWindowId']) if (Object.hasOwn(result, key)) result[key] = this.expose(result[key]);
      if (Array.isArray(result.windows)) result.windows.forEach(expose);
    };
    if (Array.isArray(frame.result)) frame.result.forEach(expose); else expose(frame.result);
    return frame;
  }
}

function frames(map) {
  let buffered = '';
  const decoder = new StringDecoder('utf8');
  return new Transform({ transform(chunk, _encoding, callback) {
    try {
      buffered += decoder.write(chunk);
      if (Buffer.byteLength(buffered) > 64 * 1024 * 1024) throw new Error('NATIVE_FRAME_LIMIT');
      let newline;
      while ((newline = buffered.indexOf('\n')) >= 0) {
        const line = buffered.slice(0, newline); buffered = buffered.slice(newline + 1);
        this.push(JSON.stringify(map(JSON.parse(line))) + '\n');
      }
      callback();
    } catch (error) { callback(error); }
  }, flush(callback) {
    buffered += decoder.end();
    callback(buffered.length ? new Error('NATIVE_FRAME_TRUNCATED') : undefined);
  } });
}

export function publishedCuaChannel(channel) {
  const tokens = new WindowTokens();
  const stdin = frames(frame => tokens.request(frame));
  const stdout = frames(frame => tokens.response(frame));
  const fail = error => { channel.close(); if (!stdout.destroyed) stdout.destroy(error); };
  stdin.on('error', fail);
  channel.stdin.on('error', fail);
  channel.stdout.on('error', fail);
  stdout.on('error', () => channel.close());
  stdin.pipe(channel.stdin);
  channel.stdout.pipe(stdout);
  return { stdin, stdout, done: channel.done,
    close: () => channel.close(), waitForClose: () => channel.waitForClose() };
}

/** Positive evidence comes from native closed frames, not Cordis's best-effort disposal. */
export function installNativeReceipt(runtime, environment = process.env) {
  const encoded = environment.JINGBAN_DSH_CUA_RECEIPT_TOKEN;
  delete environment.JINGBAN_DSH_CUA_RECEIPT_TOKEN;
  if (encoded === undefined) return runtime;
  const { path, key, nonce, session_id, run_id } = JSON.parse(encoded);
  if (typeof path !== 'string' || !isAbsolute(path) || !/^[a-f0-9]{64}$/.test(key ?? '')
      || !/^[a-f0-9]{32}$/.test(nonce ?? '') || typeof session_id !== 'string' || !session_id
      || typeof run_id !== 'string' || !run_id) {
    throw new Error('NATIVE_RECEIPT_CONFIG_INVALID');
  }
  const channels = [];
  const connect = runtime.connect.bind(runtime), dispose = runtime.dispose.bind(runtime);
  runtime.connect = (...args) => {
    const channel = connect(...args);
    const state = { closed: false };
    state.done = channel.waitForClose().then(() => { state.closed = true; });
    state.done.catch(() => {});
    channels.push(state);
    return channel;
  };
  let disposal;
  runtime.dispose = () => disposal ??= (async () => {
    let failure, failed = false;
    try { await dispose(); } catch (error) { failure = error; failed = true; }
    const results = await Promise.allSettled(channels.map(channel => channel.done));
    const rejected = results.find(result => result.status === 'rejected');
    if (rejected) { failure ??= rejected.reason; failed = true; }
    const payload = { version: 1, nonce, session_id, run_id,
      channels_started: channels.length, channels_closed: channels.filter(channel => channel.closed).length,
      disposed: true, ok: !failed };
    const receipt = { ...payload, signature: createHmac('sha256', key).update(JSON.stringify(payload)).digest('hex') };
    const temporary = `${path}.${process.pid}.tmp`;
    await writeFile(temporary, JSON.stringify(receipt), { flag: 'wx', mode: 0o600 });
    await rename(temporary, path);
    if (failed) throw failure ?? new Error('NATIVE_CLEANUP_UNCONFIRMED');
  })();
  return runtime;
}
