import type { DispatchResult } from './bridge';

export const MAX_IMAGE_INPUT_BYTES = 4 * 1024 * 1024;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
const FAILED = '图片未能添加，请重试。';
const BUSY = '正在处理，请稍后再添加图片。';
export type ImageInputContext = {
  conversationId: string; draftEpoch: string; draftDocument: string;
  supported: boolean; available: boolean;
};
type Origin = Pick<ImageInputContext, 'conversationId' | 'draftEpoch' | 'draftDocument'>;
export type ImageInputSnapshot = {
  busy: boolean; origin?: Origin; error?: Origin & { message: string };
};
export type ImageDataPayload = Origin & { filename: string; mimeType: string; dataBase64: string };
type Job = { origin: Origin; reader: FileReader | null; phase: 'reading' | 'ack'; nonce: number };

export function hasFileTransfer(transfer: Pick<DataTransfer, 'files' | 'types'> | null): boolean {
  return Boolean(transfer && (transfer.files.length || Array.from(transfer.types).includes('Files')));
}

function sameOrigin(left: Origin, right: Origin): boolean {
  return left.conversationId === right.conversationId && left.draftEpoch === right.draftEpoch
    && left.draftDocument === right.draftDocument;
}

/** Owns only event-provided files. It never reads clipboard, paths or drafts. */
export class ImageInputController {
  private job: Job | null = null;
  private error?: ImageInputSnapshot['error'];
  private nonce = 0;
  private mounted = true;

  constructor(private readonly options: {
    context: () => ImageInputContext;
    dispatch: (payload: ImageDataPayload, callback: (result: DispatchResult) => void) => boolean;
    changed: (snapshot: ImageInputSnapshot) => void;
    reader?: () => FileReader;
  }) {}

  snapshot(): ImageInputSnapshot {
    return { busy: Boolean(this.job), ...(this.job ? { origin: this.job.origin } : {}),
      ...(this.error ? { error: this.error } : {}) };
  }

  busy(): boolean { return Boolean(this.job); }

  preparingCurrent(): boolean {
    return Boolean(this.job && sameOrigin(this.job.origin, this.options.context()));
  }

  activate(): void { this.mounted = true; this.publish(); }

  dispose(): void {
    this.mounted = false;
    this.cancelReading();
    this.job = null;
    this.nonce += 1;
  }

  syncContext(): void {
    const current = this.options.context();
    if (this.job?.phase === 'reading' && !sameOrigin(this.job.origin, current)) this.cancelReading();
    // Once admitted by native, a job remains native-owned. Keep only its global
    // ACK lock; a later callback cannot change the current conversation/draft.
    if (this.error && !sameOrigin(this.error, current)) this.error = undefined;
    this.publish();
  }

  add(files: ArrayLike<File>): void {
    if (!this.mounted) return;
    const context = this.options.context();
    const fail = (message: string) => {
      this.error = { conversationId: context.conversationId, draftEpoch: context.draftEpoch,
        draftDocument: context.draftDocument, message };
      this.publish();
    };
    if (files.length !== 1) { fail('一次只能添加一张图片。'); return; }
    const file = files[0];
    let mimeType = file.type;
    // File.type may legitimately be empty. Infer only an explicit supported
    // image suffix; the native inbox still validates the actual image bytes.
    if (!mimeType || mimeType === 'application/octet-stream') {
      const extension = /\.(png|jpe?g|webp)$/i.exec(file.name)?.[1].toLowerCase();
      mimeType = extension ? `image/${extension === 'jpg' ? 'jpeg' : extension}` : '';
    }
    if (!IMAGE_TYPES.has(mimeType)) { fail('仅支持 PNG、JPEG 或 WebP 图片。'); return; }
    if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > MAX_IMAGE_INPUT_BYTES) {
      fail('请选择不超过 4 MiB 的图片。'); return;
    }
    if (!context.supported) { fail('当前电脑版本暂不支持粘贴或拖入图片，请使用添加图片。'); return; }
    if (!context.available || this.job) { fail(BUSY); return; }
    if (!context.conversationId || !context.draftEpoch || !context.draftDocument) { fail(FAILED); return; }
    const origin = { conversationId: context.conversationId, draftEpoch: context.draftEpoch,
      draftDocument: context.draftDocument };
    const nonce = ++this.nonce;
    let reader: FileReader;
    try { reader = this.options.reader?.() ?? new FileReader(); }
    catch { fail(FAILED); return; }
    this.error = undefined;
    this.job = { origin, reader, phase: 'reading', nonce };
    this.publish();
    const current = () => this.mounted && this.job?.nonce === nonce;
    const finish = (failed: boolean) => {
      if (!current()) return;
      this.job = null;
      if (this.error?.message === BUSY && sameOrigin(this.error, origin)) this.error = undefined;
      if (failed && sameOrigin(origin, this.options.context())) this.error = { ...origin, message: FAILED };
      this.publish();
    };
    reader.onerror = () => finish(true);
    reader.onabort = () => finish(false);
    reader.onload = () => {
      if (!current()) return;
      const latest = this.options.context();
      if (!sameOrigin(origin, latest) || !latest.available || !latest.supported) { finish(false); return; }
      const prefix = `data:${mimeType};base64,`;
      const result = reader.result;
      if (typeof result !== 'string' || !result.startsWith(prefix)) { finish(true); return; }
      const dataBase64 = result.slice(prefix.length);
      if (!dataBase64 || dataBase64.length > Math.ceil(MAX_IMAGE_INPUT_BYTES / 3) * 4
          || dataBase64.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(dataBase64)) { finish(true); return; }
      const fallback = `图片.${mimeType === 'image/jpeg' ? 'jpg' : mimeType.slice(6)}`;
      const filename = Array.from(file.name.replace(/[\x00-\x1f\x7f/\\]/g, '_').trim()).slice(0, 160).join('') || fallback;
      this.job = { origin, reader: null, phase: 'ack', nonce };
      // Keep the local lock until ACK. The new host publishes preparing/busy
      // on this same transport before returning its accepted ACK.
      try {
        const sent = this.options.dispatch({ ...origin, filename, mimeType, dataBase64 },
          (response) => finish(response.ok !== true));
        if (!sent) finish(true);
      } catch { finish(true); }
    };
    try { reader.readAsDataURL(file.type === mimeType ? file : new Blob([file], { type: mimeType })); }
    catch { finish(true); }
  }

  private cancelReading(): void {
    const job = this.job;
    if (job?.phase !== 'reading') return;
    this.job = null;
    this.nonce += 1;
    try { job.reader?.abort(); } catch { /* A completed reader has no effect. */ }
  }

  private publish(): void { if (this.mounted) this.options.changed(this.snapshot()); }
}
