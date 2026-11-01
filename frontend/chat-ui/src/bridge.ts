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
  conversationId: string; messageId: string; imageId: string; thumbnail?: unknown; error?: unknown;
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
  metadata?: { request_status?: string; selection_context?: SelectionSource;
    agent_error_code?: string; agent_error?: string;
    image_id?: string; image_error?: ImageErrorCode; image_unavailable?: boolean;
    image_thumbnail?: ImageThumbnail; image_thumbnail_error?: string; execution?: ExecutionTrace };
  retryAvailable?: boolean;
  retryStatus?: string;
};

export type SelectionSource = {
  text: string;
  title: string;
  url: string;
  page_characters?: number;
  notes?: string[];
};

export type ChatState = {
  conversationId: string;
  title: string;
  messages: ChatMessage[];
  conversations: { id: string; title: string; updatedAt?: string; summary?: string; messageCount?: number }[];
  busy: boolean;
  historyLoading: boolean;
  error: string;
  status: string;
  contextUsage: string;
  exportBusy: boolean;
  exportStatus: string;
  canSettings: boolean;
  windowChrome?: boolean;
  canCamera: boolean;
  canMemory: boolean;
  canExport: boolean;
  canBudget: boolean;
  ready: boolean;
  requestInCurrent: boolean;
  focusRequest?: { id: string; serial: number };
  drafts: Record<string, string>;
  stagedImage?: StagedImage;
  imagePreparing: boolean;
  imageError: string;
  errorAction: 'settings' | 'history' | 'retry' | '';
  errorConversationId: string;
  errorMessageId: string;
  requestConversationId: string;
  canStop: boolean;
  canAgentMode: boolean;
  agentMode: boolean;
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

export type DispatchResult = { ok: boolean; error?: string };
export type Dispatch = (action: string, payload?: Record<string, unknown>, callback?: (result: DispatchResult) => void) => boolean;

type Bridge = {
  getState: (callback: (json: string) => void) => void;
  dispatch: (action: string, payload: string, callback?: (result: string) => void) => void;
  stateChanged: { connect: (callback: (json: string) => void) => void; disconnect?: (callback: (json: string) => void) => void };
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
  imagePreparing: false, imageError: '',
  errorAction: '', errorConversationId: '', errorMessageId: '', requestConversationId: '', canStop: false,
  canAgentMode: false, agentMode: false,
};

export function useBridge() {
  const [state, setState] = useState<ChatState>(INITIAL);
  const [connectionError, setConnectionError] = useState('');
  const [bridge, setBridge] = useState<Bridge | null>(null);

  useEffect(() => {
    let alive = true;
    let connected: Bridge | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const receive = (json: string) => {
      if (!alive) return;
      try {
        const value = JSON.parse(json) as Partial<ChatState>;
        if (!value || !Array.isArray(value.messages) || !Array.isArray(value.conversations)) {
          throw new Error('Invalid chat state');
        }
        const messages = value.messages.map((message) => ({ ...message,
          metadata: { ...message.metadata, execution: normalizeExecution(message.metadata?.execution),
            image_thumbnail: normalizeImageThumbnail(message.metadata?.image_thumbnail),
            image_thumbnail_error: normalizeThumbnailError(message.metadata?.image_thumbnail_error) } }));
        setState({ ...INITIAL, ...value, messages, ready: value.ready === true,
          canAgentMode: value.canAgentMode === true, agentMode: value.agentMode === true,
          stagedImage: normalizeStagedImage(value.stagedImage, value.conversationId),
          imagePreparing: value.imagePreparing === true,
          imageError: typeof value.imageError === 'string' ? value.imageError.slice(0, 240) : '' });
        setConnectionError('');
      } catch {
        setConnectionError('读取对话失败，请关闭后重新打开聊天窗口。');
      }
    };
    // A separate result signal cannot be lost when native coalesces state updates
    // or evicts an older thumbnail from its bounded cache before the next snapshot.
    const receiveThumbnail = (json: string) => {
      if (!alive) return;
      try {
        const value = objectValue(JSON.parse(json));
        if (!value || typeof value.conversationId !== 'string' || typeof value.messageId !== 'string' ||
            typeof value.imageId !== 'string' || !/^[0-9a-f]{64}$/.test(value.imageId)) return;
        window.dispatchEvent(new CustomEvent(IMAGE_THUMBNAIL_EVENT, { detail: value }));
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
          connected.stateChanged.connect(receive);
          connected.thumbnailReady?.connect(receiveThumbnail);
          setBridge(connected);
          connected.getState(receive);
        });
      } else if (Date.now() - started < 10000) {
        retry = setTimeout(connect, 100);
      } else {
        setConnectionError('聊天服务未连接，请从 AURA 打开此窗口。');
      }
    };
    connect();
    return () => {
      alive = false;
      clearTimeout(retry);
      connected?.stateChanged.disconnect?.(receive);
      connected?.thumbnailReady?.disconnect?.(receiveThumbnail);
    };
  }, []);

  const dispatch = useCallback<Dispatch>((action, payload = {}, callback) => {
    if (!bridge || !state.ready) { callback?.({ ok: false }); return false; }
    bridge.dispatch(action, JSON.stringify(payload), (result) => {
      try { callback?.(JSON.parse(result) as DispatchResult); }
      catch { callback?.({ ok: false, error: '聊天服务未返回有效结果' }); }
    });
    return true;
  }, [bridge, state.ready]);

  return { state, dispatch, connectionError };
}
