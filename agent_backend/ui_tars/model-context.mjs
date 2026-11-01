/** Restore the complete current task before the pinned SDK's five-image slice. */
import { IMAGE_PLACEHOLDER } from '@ui-tars/shared/constants';

export class TaskModelContext {
  constructor() { this.clear(); }
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
        this.entries[0] = entry;
        this.originalTaskCaptured = true;
      } else this.entries.push(entry);
    }
  }
  restore(params) {
    if (!this.entries.length) return params;
    const injectedTask = params.conversations?.[0];
    const conversations = this.entries.map((entry, index) => ({ from: entry.from,
      value: index === 0 && injectedTask?.from === 'human' && typeof injectedTask.value === 'string' ? injectedTask.value : entry.value }));
    const images = this.entries.filter((entry) => entry.value === IMAGE_PLACEHOLDER && typeof entry.screenshotBase64 === 'string')
      .map((entry) => entry.screenshotBase64);
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
