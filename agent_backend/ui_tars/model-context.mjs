/** Task-local evidence working set, with explicit omission notices. */
import { IMAGE_PLACEHOLDER } from '@ui-tars/shared/constants';

export class TaskModelContext {
  constructor({ maxImages = 8, maxTextChars = 64000 } = {}) {
    if (!Number.isSafeInteger(maxImages) || maxImages < 3 || maxImages > 24 ||
        !Number.isSafeInteger(maxTextChars) || maxTextChars < 4000) throw new TypeError('Invalid task context budget');
    this.maxImages = maxImages; this.maxTextChars = maxTextChars; this.clear();
  }
  begin(task) {
    this.clear();
    this.task = task;
    this.entries.push({ from: 'human', value: task });
  }
  clear() {
    this.task = null;
    this.entries = [];
    this.seen = new WeakSet();
    this.originalTaskCaptured = false;
    this.pressure = 0;
    this.lastProjection = null;
    this.archivedImages = 0;
    this.archivedRecords = 0;
  }
  capture(conversations) {
    if (!Array.isArray(conversations)) return;
    for (const entry of conversations) {
      if (!entry || typeof entry !== 'object' || this.seen.has(entry) ||
        !['human', 'gpt'].includes(entry.from) || typeof entry.value !== 'string') continue;
      this.seen.add(entry);
      // Some SDK callbacks expose full history, others expose only a delta.
      // Replace our initial task anchor with the SDK object if it appears.
      if (!this.originalTaskCaptured && entry.from === 'human' && entry.value === this.task && !entry.screenshotBase64) {
        this.entries[0] = { ...entry };
        this.originalTaskCaptured = true;
      } else this.entries.push({ ...entry });
    }
    // The SDK itself keeps only a small image window. Do not defeat that
    // memory bound by retaining every base64 forever in this adapter.
    const shots = this.entries.map((entry, index) => ({ entry, index })).filter(({ entry }) =>
      typeof entry.screenshotBase64 === 'string');
    const keep = new Set([shots[0]?.index, ...shots.slice(-23).map(item => item.index)]);
    for (const { entry, index } of shots) if (!keep.has(index)) {
      delete entry.screenshotBase64;
      entry.value = '[Earlier screen observation omitted from the working archive; inspect current state before acting.]';
      this.archivedImages++;
    }
    if (this.entries.length > 512) {
      const removed = this.entries.splice(2, this.entries.length - 512);
      this.archivedRecords += removed.length;
    }
  }
  /** A context rejection changes only the next model request, never input. */
  reduce() { this.pressure = Math.min(this.pressure + 1, 2); }
  restore(params) {
    if (!this.entries.length) return params;
    const injectedTask = params.conversations?.[0];
    const imageEntries = this.entries.map((entry, index) => ({ entry, index })).filter(({ entry }) =>
      entry.value === IMAGE_PLACEHOLDER && typeof entry.screenshotBase64 === 'string');
    const cap = Math.max(3, Math.floor(this.maxImages / (this.pressure + 1)));
    const selected = new Set();
    const add = item => { if (item && selected.size < cap) selected.add(item.index); };
    add(imageEntries.at(-1)); add(imageEntries[0]);
    const navigation = imageEntries.filter(({ index }) =>
      /Action:\s*(?:scroll|drag|left_click_drag|hotkey)|(?:switch|navigate)/i.test(this.entries[index - 1]?.value ?? ''));
    add(navigation.at(-1)); add(navigation[0]);
    for (const item of [...imageEntries].reverse()) add(item);
    const anchor = injectedTask?.from === 'human' && typeof injectedTask.value === 'string' ? injectedTask.value : this.entries[0].value;
    const conversations = [{ from: this.entries[0].from, value: anchor }];
    const retained = new Set(); let remaining = this.maxTextChars;
    for (let index = this.entries.length - 1; index > 0; index--) {
      const entry = this.entries[index];
      if (entry.value === IMAGE_PLACEHOLDER) { if (selected.has(index)) retained.add(index); continue; }
      if (entry.value.length <= remaining) { retained.add(index); remaining -= entry.value.length; }
    }
    const omittedImages = imageEntries.length - selected.size + this.archivedImages;
    const omittedText = this.entries.filter((entry, index) => index > 0 && entry.value !== IMAGE_PLACEHOLDER && !retained.has(index)).length + this.archivedRecords;
    if (omittedImages || omittedText) conversations.push({ from: 'human', value:
      `[HOST WORKING SET: ${omittedImages} earlier images and ${omittedText} earlier action records omitted. ` +
      'Retained observations preserve the initial target, selected navigation and current state. Gaps do not prove success. ' +
      'Re-observe ambiguous targets; never repeat a prior submission because its frame is omitted.]' });
    for (let index = 1; index < this.entries.length; index++) if (retained.has(index)) {
      const entry = this.entries[index]; conversations.push({ from: entry.from, value: entry.value });
    }
    const images = imageEntries.filter(({ index }) => selected.has(index)).map(({ entry }) => entry.screenshotBase64);
    this.lastProjection = { total_images: imageEntries.length, retained_images: images.length,
      omitted_images: omittedImages, omitted_text_records: omittedText };
    return { ...params, conversations, images };
  }
}

export function isContextWindowError(exception) {
  const body = exception?.error ?? exception?.body?.error ?? exception?.body;
  if (!body || typeof body !== 'object') return false;
  const code = typeof body.code === 'string' ? body.code.toLowerCase() : '';
  if (['context_length_exceeded', 'context_window_exceeded', 'max_context_length_exceeded', 'prompt_too_long', 'too_many_input_tokens'].includes(code)) return true;
  const text = typeof body.message === 'string' ? body.message.slice(0, 8192).toLowerCase() : '';
  return ['maximum context length', 'context length exceeded', 'context window exceeded',
    'too many input tokens', 'prompt is too long', '上下文长度超', '超出上下文窗口'].some((value) => text.includes(value));
}
