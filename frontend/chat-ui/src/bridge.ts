import { useCallback, useEffect, useState } from 'react';

export type ScreenEvent = {
  source_kind?: string;
  app_name?: string;
  observed_at?: string;
  event_type?: string;
  context_summary?: string;
};

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  source?: string;
  screen_event?: ScreenEvent | null;
  metadata?: { request_status?: string; selection_context?: SelectionSource };
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
  canCamera: boolean;
  canMemory: boolean;
  canExport: boolean;
  canBudget: boolean;
  ready: boolean;
  requestInCurrent: boolean;
  focusRequest?: { id: string; serial: number };
  drafts: Record<string, string>;
  errorAction: 'settings' | 'history' | 'retry' | '';
  errorConversationId: string;
  errorMessageId: string;
  requestConversationId: string;
  canStop: boolean;
  errorRetryAvailable?: boolean;
  errorRetryStatus?: string;
  chatFontSize?: number;
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
  errorAction: '', errorConversationId: '', errorMessageId: '', requestConversationId: '', canStop: false,
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
        setState({ ...INITIAL, ...value, ready: value.ready === true });
        setConnectionError('');
      } catch {
        setConnectionError('读取对话失败，请关闭后重新打开聊天窗口。');
      }
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
