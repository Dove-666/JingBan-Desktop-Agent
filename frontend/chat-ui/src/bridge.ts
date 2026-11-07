import { useCallback, useEffect, useState } from 'react';

export type ScreenEvent = {
  source_kind?: string;
  app_name?: string;
  observed_at?: string;
  event_type?: string;
  context_summary?: string;
};

export type ImageErrorCode = 'image_missing' | 'image_unavailable' | 'vision_rejected';
export type ImageThumbnail = { src: string; width: number; height: number };
export const IMAGE_THUMBNAIL_EVENT = 'aura:image-thumbnail-ready';
export type ImageThumbnailReady = {
  conversationId: string; messageId: string; imageId: string; imageRevision?: number; thumbnail?: unknown; error?: unknown;
};
export type StagedImage = {
  conversationId: string; image_id: string; filename: string; width: number; height: number; size: number;
  thumbnail?: ImageThumbnail;
};

export type ExecutionState = 'running' | 'completed' | 'failed' | 'cancelled' | 'needs_user' | 'interrupted';
export type ExecutionEvent = {
  id: string; elapsed_ms: number; kind: 'progress' | 'tool' | 'computer' | 'summary';
  label: string; detail?: string; status: 'running' | 'completed' | 'failed';
};
export type RequestContext = {
  model: string; policy: 'model' | 'manual'; history_messages: number; estimated_input_tokens: number;
  agent_model?: string; gui_model?: string;
  model_context_tokens?: number | null; output_tokens?: number | null;
  source_counts: Record<string, number>; messages: { role: string; content: string }[];
};
export type ExecutionTrace = {
  version: 1; state: ExecutionState; started_at: string; duration_ms: number;
  events: ExecutionEvent[]; context?: RequestContext;
};

export function safeTraceText(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value.replace(/data:[^\s,<>"']{0,160},[^\s<>"']*/gi, '[已隐藏内嵌数据]')
    .replace(/\bsk-[A-Za-z0-9_-]{12,}/g, '[已隐藏密钥]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [已隐藏密钥]')
    .replace(/\b(?:api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi, '[已隐藏凭据]');
}

const finiteCount = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
const objectValue = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;

export function normalizeImageRevision(value: unknown): number | undefined {
  if (value === undefined) return 0;
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) return value;
  return undefined;
}

/** Only bounded native PNG previews may enter an img element; never URLs or paths. */
export function normalizeImageThumbnail(value: unknown): ImageThumbnail | undefined {
  const raw = objectValue(value);
  if (!raw || typeof raw.src !== 'string' || raw.src.length > 3 * 1024 * 1024 ||
      typeof raw.width !== 'number' || !Number.isInteger(raw.width) || raw.width < 1 || raw.width > 640 ||
      typeof raw.height !== 'number' || !Number.isInteger(raw.height) || raw.height < 1 || raw.height > 640 ||
      !raw.src.startsWith('data:image/png;base64,')) return;
  const data = raw.src.slice('data:image/png;base64,'.length);
  if (data.length < 44 || data.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)) return;
  try {
    const header = atob(data.slice(0, 44));
    if (header.slice(0, 8) !== '\x89PNG\r\n\x1a\n' || header.slice(12, 16) !== 'IHDR') return;
    const size = (offset: number) => ((header.charCodeAt(offset) * 0x1000000) +
      (header.charCodeAt(offset + 1) << 16) + (header.charCodeAt(offset + 2) << 8) + header.charCodeAt(offset + 3));
    if (size(16) !== raw.width || size(20) !== raw.height) return;
    return { src: raw.src, width: raw.width, height: raw.height };
  } catch { return; }
}

export function normalizeThumbnailError(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value) return;
  return value === '引用的图片不存在或已删除，请重新发送图片后重试。' ? value
    : '无法读取或校验引用的图片，请重新发送图片后重试。';
}

function normalizeStagedImage(value: unknown, conversationId: unknown): StagedImage | undefined {
  const raw = objectValue(value);
  if (!raw || raw.conversationId !== conversationId || typeof raw.conversationId !== 'string' ||
      typeof raw.image_id !== 'string' || !/^[0-9a-f]{64}$/.test(raw.image_id)) return;
  return { conversationId: raw.conversationId, image_id: raw.image_id,
    filename: typeof raw.filename === 'string' ? raw.filename.slice(0, 256) : '图片',
    width: finiteCount(raw.width), height: finiteCount(raw.height), size: finiteCount(raw.size),
    thumbnail: normalizeImageThumbnail(raw.thumbnail) };
}

/** A second allowlist at the renderer boundary; no reasoning/HTML is executed. */
export function normalizeExecution(value: unknown): ExecutionTrace | undefined {
  const raw = objectValue(value);
  if (!raw || raw.version !== 1 || !['running', 'completed', 'failed', 'cancelled', 'needs_user', 'interrupted'].includes(String(raw.state))) return;
  const events: ExecutionEvent[] = (Array.isArray(raw.events) ? raw.events : []).flatMap((entry, index) => {
    const event = objectValue(entry);
    if (!event || !['progress', 'tool', 'computer', 'summary'].includes(String(event.kind)) || !['running', 'completed', 'failed'].includes(String(event.status))) return [];
    const label = safeTraceText(event.label);
    if (!label || /^\s*(?:thought|analysis|reasoning(?:_content)?)\s*:/i.test(label)) return [];
    const detail = safeTraceText(event.detail);
    return [{ id: safeTraceText(event.id) || `event-${index}`, elapsed_ms: finiteCount(event.elapsed_ms),
      kind: event.kind as ExecutionEvent['kind'], label,
      ...(detail && !/^\s*(?:thought|analysis|reasoning(?:_content)?)\s*:/i.test(detail) ? { detail } : {}),
      status: event.status as ExecutionEvent['status'] }];
  });
  const result: ExecutionTrace = { version: 1, state: raw.state as ExecutionState,
    started_at: typeof raw.started_at === 'string' && Number.isFinite(Date.parse(raw.started_at)) ? raw.started_at : '',
    duration_ms: finiteCount(raw.duration_ms), events };
  const context = objectValue(raw.context);
  if (context) {
    const counts = objectValue(context.source_counts) ?? {};
    result.context = { model: safeTraceText(context.model), agent_model: safeTraceText(context.agent_model), gui_model: safeTraceText(context.gui_model), policy: context.policy === 'manual' ? 'manual' : 'model',
      history_messages: finiteCount(context.history_messages), estimated_input_tokens: finiteCount(context.estimated_input_tokens),
      model_context_tokens: typeof context.model_context_tokens === 'number' && context.model_context_tokens > 0 ? finiteCount(context.model_context_tokens) : null,
      output_tokens: typeof context.output_tokens === 'number' && context.output_tokens > 0 ? finiteCount(context.output_tokens) : null,
      source_counts: Object.fromEntries(Object.entries(counts).filter(([key]) => /^[a-z][a-z_]{0,39}$/.test(key)).map(([key, count]) => [key, finiteCount(count)])),
      messages: (Array.isArray(context.messages) ? context.messages : []).flatMap((entry) => {
        const message = objectValue(entry);
        if (!message || !['system', 'developer', 'user', 'assistant', 'tool'].includes(String(message.role)) || typeof message.content !== 'string') return [];
        return [{ role: String(message.role), content: safeTraceText(message.content) }];
      }),
    };
  }
  return result;
}

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  source?: string;
  screen_event?: ScreenEvent | null;
  metadata?: { request_status?: string; reply_to?: string | null; selection_context?: SelectionSource;
    agent_error_code?: string; agent_error?: string;
    image_id?: string; image_revision?: number; image_error?: ImageErrorCode; image_unavailable?: boolean;
    image_thumbnail?: ImageThumbnail; image_thumbnail_error?: string; execution?: ExecutionTrace;
    execution_context_digest?: string;
    streaming_status?: 'streaming' | 'interrupted' };
  retryAvailable?: boolean;
  retryStatus?: string;
};

export type StreamingReply = {
  conversationId: string; requestId: string; text: string; state: 'streaming' | 'interrupted';
};

export type StreamCursor = { version: 1; document: string; revision: number; sequence: number };

/** A pending preview is never conversation history or a completed assistant turn. */
export function normalizeStreamingReply(value: unknown, conversationId: unknown): StreamingReply | undefined {
  const raw = objectValue(value);
  if (!raw || typeof conversationId !== 'string' || !conversationId || raw.conversationId !== conversationId ||
      typeof raw.requestId !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(raw.requestId) ||
      typeof raw.text !== 'string' || !raw.text || raw.text.length > 1048576 ||
      !['streaming', 'interrupted'].includes(String(raw.state))) return;
  return { conversationId, requestId: raw.requestId, text: raw.text,
    state: raw.state as StreamingReply['state'] };
}

export type SelectionSource = {
  text: string;
  title: string;
  url: string;
  page_characters?: number;
  notes?: string[];
};

export type ChatState = {
  theme?: 'dark' | 'light';
  conversationId: string;
  title: string;
  messages: ChatMessage[];
  streamingReply?: StreamingReply;
  streamCursor?: StreamCursor;
  executionCursor?: StreamCursor;
  conversations: { id: string; title: string; updatedAt?: string; summary?: string; messageCount?: number }[];
  busy: boolean;
  historyLoading: boolean;
  error: string;
  status: string;
  contextUsage: string;
  exportBusy: boolean;
  exportStatus: string;
  canSettings: boolean;
  canXiangqi?: boolean;
  canGomoku?: boolean;
  windowChrome?: boolean;
  canCamera: boolean;
  canMemory: boolean;
  canExport: boolean;
  canBudget: boolean;
  ready: boolean;
  requestInCurrent: boolean;
  focusRequest?: { id: string; serial: number };
  drafts: Record<string, string>;
  draftDocument: string;
  draftEpochs: Record<string, string>;
  draftVersions: Record<string, number>;
  draftSequences: Record<string, number>;
  stagedImage?: StagedImage;
  imagePreparing: boolean;
  imageImportBusy: boolean;
  canStageImageData: boolean;
  imageError: string;
  errorAction: 'settings' | 'history' | 'retry' | '';
  errorConversationId: string;
  errorMessageId: string;
  requestConversationId: string;
  canStop: boolean;
  canAgentMode: boolean;
  agentMode: boolean;
  agentInteraction?: AgentInteraction;
  canSteerAgent?: boolean;
  agentArtifacts?: AgentArtifact[];
  canExportArtifact?: boolean;
  errorRetryAvailable?: boolean;
  errorRetryStatus?: string;
  chatFontSize?: number;
  contextPolicy?: 'model' | 'manual';
  perception?: { enabled: boolean; allowRemote: boolean; paused: boolean; busy: boolean; status: string;
    cooldownSeconds: number; hourlyRemaining: number; nextObservationSeconds: number | null;
    nextAnalysisSeconds: number; previewAgeSeconds: number | null; analysisError?: string;
    permissionSaveFailed?: boolean; persistenceWarning?: string } | null;
  camera?: { enabled: boolean; allowRemote: boolean; paused: boolean; busy: boolean; status: string;
    cooldownSeconds?: number; hourlyRemaining?: number; cameraError?: string; analysisError?: string;
    permissionSaveFailed?: boolean; persistenceWarning?: string } | null;
};

export type AgentInteraction = {
  id: string; kind: 'question' | 'file_access'; question: string; conversation_id: string;
  options?: string[]; resource?: { path: string; access: 'read' | 'write'; directory: boolean };
};
export type AgentArtifact = { reference: string; name: string; mime: string; bytes: number; sha256: string; verification_scope: string };
export function normalizeAgentArtifacts(value: unknown): AgentArtifact[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.slice(0, 100).flatMap((item) => {
    const raw = objectValue(item);
    if (!raw || typeof raw.reference !== 'string' || !/^artifact_[a-f0-9]{64}$/.test(raw.reference) || seen.has(raw.reference) ||
        typeof raw.name !== 'string' || !raw.name || raw.name.length > 480 || Array.from(raw.name).length > 240 || ['.', '..'].includes(raw.name) || /[\x00-\x1f/\\]/.test(raw.name) ||
        typeof raw.mime !== 'string' || raw.mime.length > 120 || !/^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+$/.test(raw.mime) ||
        typeof raw.bytes !== 'number' || !Number.isInteger(raw.bytes) || raw.bytes < 0 || raw.bytes > 16777216 ||
        typeof raw.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(raw.sha256) ||
        typeof raw.verification_scope !== 'string' || raw.verification_scope.length > 160 || /[\x00-\x1f]/.test(raw.verification_scope)) return [];
    seen.add(raw.reference);
    return [{ reference: raw.reference, name: raw.name, mime: raw.mime, bytes: raw.bytes, sha256: raw.sha256, verification_scope: raw.verification_scope }];
  });
}

export function normalizeAgentInteraction(value: unknown, conversationId: unknown): AgentInteraction | undefined {
  const raw = objectValue(value);
  if (!raw || raw.conversation_id !== conversationId || typeof raw.conversation_id !== 'string' ||
      typeof raw.id !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(raw.id) ||
      typeof raw.kind !== 'string' || !['question', 'file_access'].includes(raw.kind) || typeof raw.question !== 'string' ||
      raw.question.length < 1 || raw.question.length > 2000) return;
  const result: AgentInteraction = { id: raw.id, kind: raw.kind as AgentInteraction['kind'],
    question: raw.question, conversation_id: raw.conversation_id };
  if (raw.kind === 'file_access') {
    const resource = objectValue(raw.resource);
    if (!resource || typeof resource.path !== 'string' || !resource.path || resource.path.length > 4096 ||
        /[\x00-\x1f]/.test(resource.path) || typeof resource.access !== 'string' || !['read', 'write'].includes(resource.access) ||
        typeof resource.directory !== 'boolean') return;
    result.resource = { path: resource.path, access: resource.access as 'read' | 'write', directory: resource.directory };
  } else if (raw.options !== undefined) {
    if (!Array.isArray(raw.options) || raw.options.length > 8 || raw.options.some((item) => typeof item !== 'string' || !item || item.length > 240)) return;
    result.options = raw.options as string[];
  }
  return result;
}

export type DispatchResult = { ok: boolean; error?: string; status?: 'saved' | 'cancelled' | 'failed' };
export type Dispatch = (action: string, payload?: Record<string, unknown>, callback?: (result: DispatchResult) => void) => boolean;

type Bridge = {
  getState: (callback: (json: string) => void) => void;
  dispatch: (action: string, payload: string, callback?: (result: string) => void) => void;
  stateChanged: { connect: (callback: (json: string) => void) => void; disconnect?: (callback: (json: string) => void) => void };
  streamingChanged?: { connect: (callback: (json: string) => void) => void; disconnect?: (callback: (json: string) => void) => void };
  executionChanged?: { connect: (callback: (json: string) => void) => void; disconnect?: (callback: (json: string) => void) => void };
  thumbnailReady?: { connect: (callback: (json: string) => void) => void; disconnect?: (callback: (json: string) => void) => void };
};

declare global {
  interface Window {
    qt?: { webChannelTransport: unknown };
    QWebChannel?: new (transport: unknown, callback: (channel: { objects: { bridge: Bridge } }) => void) => unknown;
  }
}

const INITIAL: ChatState = {
  conversationId: '', title: '', messages: [], conversations: [], busy: false,
  historyLoading: false, error: '', status: '', contextUsage: '', exportBusy: false,
  exportStatus: '', canSettings: false, canCamera: false, canMemory: false, canExport: false,
  canBudget: false, ready: false, requestInCurrent: true, drafts: {},
  draftEpochs: {}, draftVersions: {}, draftSequences: {}, draftDocument: '',
  imagePreparing: false, imageImportBusy: false, canStageImageData: false, imageError: '',
  errorAction: '', errorConversationId: '', errorMessageId: '', requestConversationId: '', canStop: false,
  canAgentMode: false, agentMode: false,
};

function normalizeChatState(value: Partial<ChatState>): ChatState {
  const userIds = new Set((value.messages || []).filter((message) => message.role === 'user' &&
    (!message.source || message.source === 'user') && typeof message.id === 'string' && message.id).map((message) => message.id));
  const messages = (value.messages || []).map((message) => ({ ...message,
    metadata: { ...message.metadata, execution: normalizeExecution(message.metadata?.execution),
      ...(message.metadata?.reply_to !== undefined ? { reply_to: message.role === 'assistant' &&
        (!message.source || message.source === 'assistant') && typeof message.metadata.reply_to === 'string' &&
        userIds.has(message.metadata.reply_to) ? message.metadata.reply_to : null } : {}),
      execution_context_digest: typeof message.metadata?.execution_context_digest === 'string' &&
        /^[a-f0-9]{64}$/.test(message.metadata.execution_context_digest) ? message.metadata.execution_context_digest : undefined,
      image_revision: normalizeImageRevision(message.metadata?.image_revision) ?? 0,
      image_thumbnail: normalizeImageThumbnail(message.metadata?.image_thumbnail),
      image_thumbnail_error: normalizeThumbnailError(message.metadata?.image_thumbnail_error) } }));
  return { ...INITIAL, ...value, messages, ready: value.ready === true,
    theme: value.theme === 'dark' ? 'dark' : 'light',
    canAgentMode: value.canAgentMode === true, agentMode: value.agentMode === true,
    agentInteraction: normalizeAgentInteraction(value.agentInteraction, value.conversationId),
    streamingReply: normalizeStreamingReply(value.streamingReply, value.conversationId),
    streamCursor: normalizeStreamCursor(value.streamCursor),
    executionCursor: normalizeStreamCursor(value.executionCursor),
    agentArtifacts: normalizeAgentArtifacts(value.agentArtifacts),
    canExportArtifact: value.canExportArtifact === true,
    canSteerAgent: value.canSteerAgent === true && value.requestInCurrent === true,
    stagedImage: normalizeStagedImage(value.stagedImage, value.conversationId),
    imagePreparing: value.imagePreparing === true,
    imageImportBusy: typeof value.imageImportBusy === 'boolean' ? value.imageImportBusy : value.imagePreparing === true,
    canStageImageData: value.canStageImageData === true,
    imageError: typeof value.imageError === 'string' ? value.imageError.slice(0, 240) : '' };
}

export function normalizeStreamCursor(value: unknown): StreamCursor | undefined {
  const raw = objectValue(value);
  if (raw?.version !== 1 || typeof raw.document !== 'string' || !/^[0-9a-f]{32}$/.test(raw.document) ||
      typeof raw.revision !== 'number' || !Number.isSafeInteger(raw.revision) || raw.revision < 0 ||
      typeof raw.sequence !== 'number' || !Number.isSafeInteger(raw.sequence) || raw.sequence < 0) return;
  return { version: 1, document: raw.document, revision: raw.revision, sequence: raw.sequence };
}

type StreamPatch = StreamCursor & { conversationId: string; streamingReply: StreamingReply };
type ExecutionPatch = StreamCursor & {
  conversationId: string; messageId: string; contextDigest: string; execution: ExecutionTrace;
};
type StreamResult = { changed: boolean; state?: ChatState; requestSnapshot?: number };
type WireChatState = Partial<ChatState> & Pick<ChatState, 'messages' | 'conversations' | 'conversationId' | 'draftDocument'>;
function readFullState(value: unknown): WireChatState | undefined {
  const raw = objectValue(value);
  if (!raw || !Array.isArray(raw.messages) || !Array.isArray(raw.conversations) ||
      typeof raw.conversationId !== 'string') return;
  // Backends predating document nonces still use the full-state-only route.
  return { ...raw, draftDocument: typeof raw.draftDocument === 'string' ? raw.draftDocument : '' } as WireChatState;
}
function readStreamPatch(value: unknown): StreamPatch | undefined {
  const raw = objectValue(value), cursor = normalizeStreamCursor(value);
  const reply = normalizeStreamingReply(raw?.streamingReply, raw?.conversationId);
  // New requests, interruptions and clears are full-state barriers.
  if (!cursor || !reply || reply.state !== 'streaming') return;
  return { ...cursor, conversationId: reply.conversationId, streamingReply: reply };
}

function readExecutionPatch(value: unknown): ExecutionPatch | undefined {
  const raw = objectValue(value), cursor = normalizeStreamCursor(value);
  const content = objectValue(raw?.execution);
  if (!cursor || !raw || !content || 'context' in content || typeof raw.conversationId !== 'string' || !raw.conversationId ||
      typeof raw.messageId !== 'string' || !raw.messageId || raw.messageId.length > 200 || /[\x00-\x1f]/.test(raw.messageId) ||
      typeof raw.contextDigest !== 'string' || !/^[a-f0-9]{64}$/.test(raw.contextDigest)) return;
  const execution = normalizeExecution(content);
  if (!execution || execution.state !== 'running' || !execution.started_at) return;
  return { ...cursor, conversationId: raw.conversationId, messageId: raw.messageId,
    contextDigest: raw.contextDigest, execution };
}

/** Ordered, document-owned transport state; no draft or send authority lives here. */
export class ChatStreamProtocol {
  private seeded = false;
  private alive = true;
  private anchor = '';
  private v1 = false;
  private executionV1 = false;
  private head?: ChatState;
  private cursor?: StreamCursor;
  private executionCursor?: StreamCursor;
  private earlyFull?: WireChatState;
  private pending?: StreamPatch;
  private pendingExecution?: ExecutionPatch;
  private ticket = 0;
  private waiting?: number;
  private requestedRevision = -1;

  constructor(private readonly hasStreamSignal: boolean, private readonly hasExecutionSignal = false) {}
  get current(): ChatState | undefined { return this.head; }
  get negotiatedCapability(): 1 | undefined { return this.v1 ? 1 : undefined; }
  get bufferedPatches(): number { return Number(!!this.pending) + Number(!!this.pendingExecution); }
  get snapshotInFlight(): boolean { return this.waiting !== undefined; }

  private normalizeFull(state: WireChatState): ChatState {
    const result = normalizeChatState(state);
    if (!this.executionV1) result.executionCursor = undefined;
    return result;
  }

  private finish(changed = false): StreamResult {
    const result: StreamResult = { changed, ...(changed ? { state: this.head } : {}) };
    const missingRevision = Math.max(
      this.pending && this.cursor && this.pending.revision > this.cursor.revision ? this.pending.revision : -1,
      this.pendingExecution && this.executionCursor && this.pendingExecution.revision > this.executionCursor.revision
        ? this.pendingExecution.revision : -1);
    if (this.waiting === undefined && missingRevision > this.requestedRevision) {
      // One read per missing barrier; a stale response cannot create a loop.
      this.waiting = ++this.ticket;
      this.requestedRevision = missingRevision;
      result.requestSnapshot = this.waiting;
    }
    return result;
  }

  initial(value: unknown): StreamResult {
    if (!this.alive || this.seeded) return { changed: false };
    const state = readFullState(value);
    if (!state) return { changed: false };
    this.seeded = true;
    // Signals can arrive before initial getState. Only its owned reply anchors
    // the nonce; another document's late signal cannot replace the anchor.
    this.anchor = state.draftDocument;
    const cursor = normalizeStreamCursor(state.streamCursor);
    this.v1 = this.hasStreamSignal && !!cursor && cursor.document === this.anchor;
    this.cursor = this.v1 ? cursor : undefined;
    const executionCursor = normalizeStreamCursor(state.executionCursor);
    this.executionV1 = this.hasExecutionSignal && !!executionCursor && executionCursor.document === this.anchor &&
      (!this.v1 || executionCursor.revision === cursor?.revision);
    this.executionCursor = this.executionV1 ? executionCursor : undefined;
    this.head = this.normalizeFull(state);
    const early = this.earlyFull;
    this.earlyFull = undefined;
    const foreignEarly = !!this.anchor && (!!early && early.draftDocument !== this.anchor ||
      !!this.pendingExecution && this.pendingExecution.document !== this.anchor);
    const earlyRequest = early ? this.full(early).requestSnapshot : undefined;
    if (!this.v1) this.pending = undefined;
    if (!this.executionV1) this.pendingExecution = undefined;
    this.applyBuffered();
    const result = this.finish(true);
    if (earlyRequest !== undefined && this.waiting === earlyRequest) result.requestSnapshot = earlyRequest;
    // A foreign high revision may have displaced this document's newer full
    // before its initial read anchored the nonce. Repair once, sharing any
    // existing gap recovery; never adopt or keep retrying the foreign frame.
    if (foreignEarly && this.waiting === undefined) {
      this.waiting = ++this.ticket;
      result.requestSnapshot = this.waiting;
    }
    return result;
  }

  full(value: unknown): StreamResult {
    if (!this.alive) return { changed: false };
    const state = readFullState(value);
    if (!state) return { changed: false };
    if (!this.seeded) {
      const nextText = normalizeStreamCursor(state.streamCursor), oldText = normalizeStreamCursor(this.earlyFull?.streamCursor);
      const nextExecution = this.hasExecutionSignal ? normalizeStreamCursor(state.executionCursor) : undefined;
      const oldExecution = this.hasExecutionSignal ? normalizeStreamCursor(this.earlyFull?.executionCursor) : undefined;
      const executionOnly = this.hasExecutionSignal && !this.hasStreamSignal;
      const next = executionOnly ? nextExecution ?? nextText : nextText ?? nextExecution;
      const old = executionOnly ? oldExecution ?? oldText : oldText ?? oldExecution;
      const newerSequence = next && old && next.revision === old.revision && (
        !executionOnly && nextText && oldText && nextText.document === oldText.document && nextText.sequence > oldText.sequence ||
        nextExecution && oldExecution && nextExecution.document === oldExecution.document && nextExecution.sequence > oldExecution.sequence);
      if (!old || next && (next.revision > old.revision || newerSequence)) this.earlyFull = state;
      return { changed: false };
    }
    if (this.anchor && state.draftDocument !== this.anchor) return { changed: false };
    if (!this.v1 && !this.executionV1) {
      this.head = this.normalizeFull(state);
      return this.finish(true);
    }
    const cursor = normalizeStreamCursor(state.streamCursor);
    const executionCursor = normalizeStreamCursor(state.executionCursor);
    if (this.v1 && this.executionV1 && cursor?.revision !== executionCursor?.revision) return this.finish();
    if (this.v1 && (!cursor || cursor.document !== this.anchor || !this.cursor ||
        cursor.revision < this.cursor.revision || cursor.sequence < this.cursor.sequence)) return this.finish();
    if (this.executionV1 && (!executionCursor || executionCursor.document !== this.anchor || !this.executionCursor ||
        executionCursor.revision < this.executionCursor.revision || executionCursor.sequence < this.executionCursor.sequence)) return this.finish();
    const newerText = this.v1 && cursor && this.cursor &&
      (cursor.revision > this.cursor.revision || cursor.sequence > this.cursor.sequence);
    const newerExecution = this.executionV1 && executionCursor && this.executionCursor &&
      (executionCursor.revision > this.executionCursor.revision || executionCursor.sequence > this.executionCursor.sequence);
    if (!newerText && !newerExecution) return this.finish();
    // Check ordering before touching the potentially large message tree.
    this.head = this.normalizeFull(state);
    this.cursor = this.v1 ? cursor : undefined;
    this.executionCursor = this.executionV1 ? executionCursor : undefined;
    this.applyBuffered();
    return this.finish(true);
  }

  patch(value: unknown): StreamResult {
    if (!this.alive || !this.hasStreamSignal) return { changed: false };
    const patch = readStreamPatch(value);
    if (!patch) return { changed: false };
    if (!this.seeded) {
      this.buffer(patch);
      return { changed: false };
    }
    if (!this.v1 || patch.document !== this.anchor || !this.cursor ||
        patch.revision < this.cursor.revision || patch.sequence <= this.cursor.sequence) return { changed: false };
    if (patch.revision > this.cursor.revision) {
      this.buffer(patch);
      return this.finish();
    }
    return this.finish(this.apply(patch));
  }

  execution(value: unknown): StreamResult {
    if (!this.alive || !this.hasExecutionSignal) return { changed: false };
    const patch = readExecutionPatch(value);
    if (!patch) return { changed: false };
    if (!this.seeded) {
      this.bufferExecution(patch);
      return { changed: false };
    }
    if (!this.executionV1 || patch.document !== this.anchor || !this.executionCursor ||
        patch.revision < this.executionCursor.revision || patch.sequence <= this.executionCursor.sequence) return { changed: false };
    if (patch.revision > this.executionCursor.revision) {
      this.bufferExecution(patch);
      return this.finish();
    }
    return this.finish(this.applyExecution(patch));
  }

  recovery(ticket: number, value: unknown): StreamResult {
    if (!this.alive || ticket !== this.waiting) return { changed: false };
    this.waiting = undefined;
    return this.full(value);
  }

  private buffer(patch: StreamPatch): void {
    if (!this.pending || patch.revision > this.pending.revision ||
        patch.revision === this.pending.revision && patch.sequence > this.pending.sequence) this.pending = patch;
  }

  private bufferExecution(patch: ExecutionPatch): void {
    if (!this.pendingExecution || patch.revision > this.pendingExecution.revision ||
        patch.revision === this.pendingExecution.revision && patch.sequence > this.pendingExecution.sequence) this.pendingExecution = patch;
  }

  private applyBuffered(): void {
    if (this.pending && this.cursor) {
      if (this.pending.document !== this.anchor || this.pending.revision < this.cursor.revision) {
        this.pending = undefined;
      } else if (this.pending.revision === this.cursor.revision) {
        const patch = this.pending;
        this.pending = undefined;
        this.apply(patch);
      }
    }
    if (this.pendingExecution && this.executionCursor) {
      if (this.pendingExecution.document !== this.anchor || this.pendingExecution.revision < this.executionCursor.revision) {
        this.pendingExecution = undefined;
      } else if (this.pendingExecution.revision === this.executionCursor.revision) {
        const patch = this.pendingExecution;
        this.pendingExecution = undefined;
        this.applyExecution(patch);
      }
    }
  }

  private apply(patch: StreamPatch): boolean {
    const prior = this.head?.streamingReply;
    if (!this.head || !this.cursor || patch.document !== this.anchor ||
        patch.revision !== this.cursor.revision || patch.sequence <= this.cursor.sequence ||
        patch.conversationId !== this.head.conversationId || !this.head.busy || !this.head.requestInCurrent ||
        prior?.state !== 'streaming' || prior.requestId !== patch.streamingReply.requestId) return false;
    this.cursor = { version: 1, document: patch.document, revision: patch.revision, sequence: patch.sequence };
    this.head = { ...this.head, streamCursor: this.cursor, streamingReply: patch.streamingReply };
    return true; // messages/conversations retain the same object references.
  }

  private applyExecution(patch: ExecutionPatch): boolean {
    if (!this.head || !this.executionCursor || patch.document !== this.anchor ||
        patch.revision !== this.executionCursor.revision || patch.sequence <= this.executionCursor.sequence ||
        patch.conversationId !== this.head.conversationId) return false;
    const index = this.head.messages.findIndex((message) => message.id === patch.messageId);
    const message = this.head.messages[index], previous = message?.metadata?.execution;
    if (!previous || previous.state !== 'running' || previous.started_at !== patch.execution.started_at ||
        previous.duration_ms > patch.execution.duration_ms || message.metadata?.execution_context_digest !== patch.contextDigest) return false;
    const messages = this.head.messages.slice();
    messages[index] = { ...message, metadata: { ...message.metadata,
      execution: { ...patch.execution, ...(previous.context ? { context: previous.context } : {}) } } };
    this.executionCursor = { version: 1, document: patch.document, revision: patch.revision, sequence: patch.sequence };
    this.head = { ...this.head, executionCursor: this.executionCursor, messages };
    return true;
  }

  dispose(): void {
    this.alive = false;
    this.pending = undefined;
    this.pendingExecution = undefined;
    this.earlyFull = undefined;
    this.waiting = undefined;
  }
}

export function useBridge() {
  const [state, setState] = useState<ChatState>(INITIAL);
  const [connectionError, setConnectionError] = useState('');
  const [bridge, setBridge] = useState<Bridge | null>(null);

  useEffect(() => {
    let alive = true;
    let connected: Bridge | null = null;
    let protocol: ChatStreamProtocol | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const publish = (result: StreamResult) => {
      if (!alive) return;
      if (result.changed && result.state) {
        setState(result.state);
        setConnectionError('');
      }
      if (result.requestSnapshot !== undefined && connected && protocol) {
        const owner = connected, ordered = protocol, ticket = result.requestSnapshot;
        owner.getState((json) => {
          if (!alive || connected !== owner || protocol !== ordered) return;
          try {
            const value = JSON.parse(json);
            if (!readFullState(value)) throw new Error('Invalid chat state');
            publish(ordered.recovery(ticket, value));
          }
          catch { setConnectionError('读取对话失败，请关闭后重新打开聊天窗口。'); }
        });
      }
    };
    const receive = (json: string) => {
      if (!alive) return;
      try {
        const value = JSON.parse(json);
        if (!readFullState(value)) throw new Error('Invalid chat state');
        if (protocol) publish(protocol.full(value));
      } catch {
        setConnectionError('读取对话失败，请关闭后重新打开聊天窗口。');
      }
    };
    const receiveStream = (json: string) => {
      if (!alive || !protocol) return;
      try { publish(protocol.patch(JSON.parse(json))); }
      catch { /* Invalid patches never clear or replace the current full state. */ }
    };
    const receiveExecution = (json: string) => {
      if (!alive || !protocol) return;
      try { publish(protocol.execution(JSON.parse(json))); }
      catch { /* A malformed progress packet leaves the current view intact. */ }
    };
    // A separate result signal cannot be lost when native coalesces state updates
    // or evicts an older thumbnail from its bounded cache before the next snapshot.
    const receiveThumbnail = (json: string) => {
      if (!alive) return;
      try {
        const value = objectValue(JSON.parse(json));
        if (!value || typeof value.conversationId !== 'string' || typeof value.messageId !== 'string' ||
            typeof value.imageId !== 'string' || !/^[0-9a-f]{64}$/.test(value.imageId)) return;
        const imageRevision = normalizeImageRevision(value.imageRevision);
        if (imageRevision === undefined) return;
        window.dispatchEvent(new CustomEvent(IMAGE_THUMBNAIL_EVENT, { detail: { ...value, imageRevision } }));
      } catch { /* Invalid result messages never enter the renderer. */ }
    };
    const started = Date.now();
    const connect = () => {
      if (!alive) return;
      if (window.qt?.webChannelTransport && window.QWebChannel) {
        new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
          if (!alive) return;
          connected = channel.objects.bridge;
          if (!connected) {
            setConnectionError('聊天服务未连接，请重新打开窗口。');
            return;
          }
          protocol = new ChatStreamProtocol(!!connected.streamingChanged, !!connected.executionChanged);
          connected.stateChanged.connect(receive);
          connected.streamingChanged?.connect(receiveStream);
          connected.executionChanged?.connect(receiveExecution);
          connected.thumbnailReady?.connect(receiveThumbnail);
          setBridge(connected);
          const owner = connected, ordered = protocol;
          owner.getState((json) => {
            if (!alive || connected !== owner || protocol !== ordered) return;
            try {
              const value = JSON.parse(json);
              if (!readFullState(value)) throw new Error('Invalid chat state');
              publish(ordered.initial(value));
            }
            catch { setConnectionError('读取对话失败，请关闭后重新打开聊天窗口。'); }
          });
        });
      } else if (Date.now() - started < 10000) {
        retry = setTimeout(connect, 100);
      } else {
        setConnectionError('聊天服务未连接，请从鲸伴打开此窗口。');
      }
    };
    connect();
    return () => {
      alive = false;
      clearTimeout(retry);
      protocol?.dispose();
      connected?.stateChanged.disconnect?.(receive);
      connected?.streamingChanged?.disconnect?.(receiveStream);
      connected?.executionChanged?.disconnect?.(receiveExecution);
      connected?.thumbnailReady?.disconnect?.(receiveThumbnail);
    };
  }, []);

  const streamProtocol = bridge?.streamingChanged && state.streamCursor?.version === 1 ? 1 : undefined;
  const executionProtocol = bridge?.executionChanged && state.executionCursor?.version === 1 ? 1 : undefined;
  const dispatch = useCallback<Dispatch>((action, payload = {}, callback) => {
    if (!bridge || !state.ready) { callback?.({ ok: false }); return false; }
    // The listener is attached before acknowledgement. A legacy/nonce-less
    // ready stays a read-only refresh and never opts into this capability.
    const data = action === 'ready' && state.draftDocument && payload.draftDocument === state.draftDocument
      ? { ...payload, ...(streamProtocol === 1 ? { streamProtocol: 1 } : {}),
        ...(executionProtocol === 1 ? { executionProtocol: 1 } : {}) } : payload;
    bridge.dispatch(action, JSON.stringify(data), (result) => {
      try { callback?.(JSON.parse(result) as DispatchResult); }
      catch { callback?.({ ok: false, error: '聊天服务未返回有效结果' }); }
    });
    return true;
  }, [bridge, state.ready, streamProtocol, executionProtocol, state.draftDocument]);

  return { state, dispatch, connectionError };
}
