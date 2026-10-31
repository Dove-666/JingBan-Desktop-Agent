import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, FormEvent, KeyboardEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  AssistantRuntimeProvider, MessagePrimitive, ThreadPrimitive,
  useExternalStoreRuntime, useAuiState,
} from '@assistant-ui/react';
import type { AppendMessage, ThreadMessageLike } from '@assistant-ui/react';
import {
  ArrowDown, ArrowUp, BookOpen, Camera, Check, ChevronDown, ChevronRight, ChevronUp, Copy,
  Download, LoaderCircle, Menu, MoreHorizontal, PanelLeftClose,
  Pencil, Plus, Search, Settings2, SlidersHorizontal, Square, Trash2, X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useBridge } from './bridge';
import type { Dispatch, ScreenEvent, SelectionSource } from './bridge';

type ModalState = { kind: 'rename'; id: string; title: string } | null;
type PendingSend = { conversationId: string; text: string };
const HostContext = createContext<{ dispatch: Dispatch; conversationId: string; busy: boolean;
  focusRequest?: { id: string; serial: number }; findMatches?: Set<string>; findMatchId?: string }>({ dispatch: () => false, conversationId: '', busy: false });

function ExternalLink({ href, children }: { href?: string; children: ReactNode }) {
  const { dispatch } = useContext(HostContext);
  const [copied, setCopied] = useState(false);
  let url = '';
  try {
    if (href && !/[\u0000-\u0020\u007f]/.test(href)) {
      const parsed = new URL(href);
      if (['http:', 'https:'].includes(parsed.protocol) && parsed.hostname && !parsed.username && !parsed.password) url = parsed.href;
    }
  } catch { /* Unsupported links stay readable text. */ }
  if (!url) return <span>{children}</span>;
  return <span className="external-link"><a className="message-link" href={url} title={url}
    onClick={(event) => { event.preventDefault(); dispatch('openExternal', { url }); }}
    onAuxClick={(event) => { event.preventDefault(); if (event.button === 1) dispatch('openExternal', { url }); }}>{children}</a>
    <button type="button" className="link-copy" aria-label={`复制链接地址：${url}`} title={copied ? '已复制链接' : '复制链接地址'}
      onClick={() => dispatch('copyLink', { url }, (result) => setCopied(result.ok))}>{copied ? <Check size={12} /> : <Copy size={12} />}</button></span>;
}

function IconButton({ children, label, className = '', ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button type="button" className={`icon-button ${className}`} aria-label={label} title={label} {...props}>{children}</button>;
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const { dispatch } = useContext(HostContext);
  const code = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return <div className="code-block"><div className="code-toolbar">
    <button type="button" aria-label={copied ? '代码已复制' : '复制代码'} onClick={() => {
      dispatch('copy', { text: code.current?.textContent || '' }, (result) => {
        if (!result.ok) return;
        setCopied(true); clearTimeout(timer.current); timer.current = setTimeout(() => setCopied(false), 1800);
      });
    }}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? '已复制' : '复制代码'}</button>
  </div><pre ref={code} tabIndex={0}>{children}</pre></div>;
}

const MARKDOWN_COMPONENTS: Components = {
  a: ({ href, children }) => <ExternalLink href={href}>{children}</ExternalLink>,
  table: ({ children }) => <div className="table-scroll" tabIndex={0}><table>{children}</table></div>,
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
};

function Markdown({ text }: { text: string }) {
  return <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    skipHtml
    allowedElements={['p', 'br', 'strong', 'em', 'del', 'blockquote', 'ul', 'ol', 'li', 'hr', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a']}
    components={MARKDOWN_COMPONENTS}
  >{text}</ReactMarkdown>;
}

function ChatMessageView() {
  const message = useAuiState((value) => value.message);
  const { dispatch, focusRequest, conversationId, busy, findMatches, findMatchId } = useContext(HostContext);
  const text = message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n');
  const meta = message.metadata.custom as { source?: string; screen_event?: ScreenEvent; selection_context?: SelectionSource; request_status?: string; retryAvailable?: boolean; retryStatus?: string };
  const isUser = message.role === 'user';
  const isCamera = meta?.source === 'proactive_camera' || meta?.screen_event?.source_kind === 'camera';
  const isPerception = isCamera || meta?.source === 'proactive_screen';
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  useEffect(() => { if (focusRequest?.id === message.id) setExpanded(true); }, [focusRequest?.id, focusRequest?.serial, message.id]);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(copyTimer.current), []);
  const copy = () => {
    dispatch('copy', { text }, (result) => {
      if (result.ok) {
        setCopied(true);
        clearTimeout(copyTimer.current);
        copyTimer.current = setTimeout(() => setCopied(false), 1800);
      }
    });
  };
  return <MessagePrimitive.Root className={`message ${isUser ? 'message-user' : 'message-assistant'} ${findMatches?.has(message.id) ? 'message-find-match' : ''} ${findMatchId === message.id ? 'message-find-current' : ''}`} data-message-id={message.id}>
    <div className="message-body">
      {!isUser && isPerception && meta.screen_event && <div className="screen-event" data-source-kind={isCamera ? 'camera' : 'screen'}>
        <button className="event-toggle" data-testid="screen-event-toggle" type="button" onClick={() => {
          setExpanded((value) => !value);
          if (!expanded) dispatch('focusEvent', { id: message.id });
        }} aria-expanded={expanded}>
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span>{isCamera ? '来自摄像头识别' : '来自屏幕感知'}</span>
        </button>
        {expanded && <div className="event-details" data-testid="screen-event-details">
          <div className="event-app">{isCamera ? `摄像头情境${meta.screen_event.app_name ? ` · ${meta.screen_event.app_name}` : ''}` : meta.screen_event.app_name || '屏幕情境'}<span>{meta.screen_event.observed_at || ''}</span></div>
          <p>{meta.screen_event.context_summary || '暂无情境摘要'}</p>
          {meta.screen_event.event_type && <span className="event-type">{meta.screen_event.event_type}</span>}
        </div>}
      </div>}
      <div className="markdown"><Markdown text={text} /></div>
      {isUser && meta.selection_context && <details className="selection-source" data-testid="selection-source">
        <summary>引用选区 · {meta.selection_context.title || '当前页面'}</summary>
        <p>{meta.selection_context.text}</p>
        {meta.selection_context.url && <ExternalLink href={meta.selection_context.url}>查看来源页面</ExternalLink>}
        <small>{meta.selection_context.page_characters ? `已附带 ${meta.selection_context.page_characters} 字页面参考，模型请求按预算截取。` : '本次只附带所选文字。'}</small>
        {meta.selection_context.notes?.map((note, index) => <small key={index}>{note}</small>)}
      </details>}
      {isUser && ['failed', 'cancelled', 'interrupted', 'pending'].includes(meta?.request_status || '') &&
        <div className={`request-message-state request-${meta.request_status}`} role="status" data-testid="message-request-state">
          <span>{({ failed: '回复失败', cancelled: '已停止生成', interrupted: '上次请求未完成', pending: '正在等待回复' } as Record<string, string>)[meta.request_status!]}</span>
          {meta.request_status !== 'pending' && <button type="button" disabled={busy || meta.retryAvailable === false} onClick={() =>
            dispatch('retry', { conversationId, messageId: message.id })}>重试回复</button>}
          {meta.retryStatus && <span className="retry-explanation">{meta.retryStatus}</span>}
        </div>}
      <div className="message-actions">
        <IconButton label={copied ? '已复制' : '复制消息'} onClick={copy}>{copied ? <Check size={15} /> : <Copy size={15} />}</IconButton>
      </div>
    </div>
  </MessagePrimitive.Root>;
}

function Modal({ children, title, onClose }: { children: ReactNode; title: string; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    const field = dialog?.querySelector<HTMLInputElement>('input:not(:disabled)');
    field?.focus(); field?.select();
    const trap = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Tab' || !dialog?.open) return;
      const controls = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]')]
        .filter((control) => control.getClientRects().length > 0);
      const index = controls.indexOf(document.activeElement as HTMLElement);
      if (!controls.length) { event.preventDefault(); return; }
      if (index < 0 || event.shiftKey && index === 0 || !event.shiftKey && index === controls.length - 1) {
        event.preventDefault(); (event.shiftKey ? controls[controls.length - 1] : controls[0]).focus();
      }
    };
    document.addEventListener('keydown', trap, true);
    return () => document.removeEventListener('keydown', trap, true);
  }, []);
  return <dialog ref={ref} className="dialog" aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); onClose(); }} onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="dialog-heading"><h2 id={titleId}>{title}</h2><IconButton label="关闭" onClick={onClose}><X size={18} /></IconButton></div>
    {children}
  </dialog>;
}

export function App() {
  const { state, dispatch, connectionError } = useBridge();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [desktopSidebar, setDesktopSidebar] = useState(true);
  const [searching, setSearching] = useState(false);
  const [search, setSearch] = useState('');
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState('');
  const [findPosition, setFindPosition] = useState(0);
  const [findNavigation, setFindNavigation] = useState(0);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [rowMenuPosition, setRowMenuPosition] = useState({ left: 0, top: 0 });
  const [modal, setModal] = useState<ModalState>(null);
  const [renameValue, setRenameValue] = useState('');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<PendingSend | null>(null);
  const [showLatest, setShowLatest] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const findInput = useRef<HTMLInputElement>(null);
  const renameComposition = useRef(false);
  const sidebar = useRef<HTMLElement>(null);
  const main = useRef<HTMLElement>(null);
  const drawerOrigin = useRef<HTMLElement | null>(null);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 760px)').matches);
  const viewport = useRef<HTMLDivElement>(null);
  const readingColumn = useRef<HTMLDivElement>(null);
  const composerArea = useRef<HTMLDivElement>(null);
  const [composerHeight, setComposerHeight] = useState(133);
  const nearBottom = useRef(true);
  const previousConversation = useRef('');
  const composition = useRef(false);
  const searchStarted = useRef(false);
  const readySent = useRef(false);
  const hydrated = useRef(false);
  const appliedFocus = useRef('');
  const focusHighlightNode = useRef<HTMLElement | null>(null);
  const focusHighlightTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const draft = drafts[state.conversationId] || '';
  const canSend = state.ready && !state.busy && !state.historyLoading && !pending && Boolean(draft.trim());
  const findIds = useMemo(() => {
    const query = findQuery.trim().toLocaleLowerCase();
    return findOpen && query ? state.messages.filter((message) =>
      message.content.toLocaleLowerCase().includes(query)).map((message) => message.id) : [];
  }, [findOpen, findQuery, state.messages]);
  const findMatches = useMemo(() => new Set(findIds), [findIds]);
  const findIndex = Math.min(findPosition, Math.max(0, findIds.length - 1));
  const findMatchId = findIds[findIndex];
  const moveFind = useCallback((direction: number) => {
    if (!findIds.length) return;
    setFindPosition((value) => (Math.min(value, findIds.length - 1) + direction + findIds.length) % findIds.length);
    setFindNavigation((value) => value + 1);
  }, [findIds.length]);
  const openFind = useCallback(() => {
    setFindOpen(true); setToolsOpen(false); setRowMenu(null); setSidebarOpen(false);
    requestAnimationFrame(() => { findInput.current?.focus(); findInput.current?.select(); });
  }, []);
  const closeFind = useCallback(() => { setFindOpen(false); editor.current?.focus(); }, []);

  useEffect(() => {
    setFindOpen(false); setFindQuery(''); setFindPosition(0);
  }, [state.conversationId]);

  useEffect(() => {
    if (!findOpen || !findMatchId || state.historyLoading) return;
    const frame = requestAnimationFrame(() => {
      const node = viewport.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(findMatchId)}"]`);
      if (node) {
        nearBottom.current = false;
        node.scrollIntoView({ block: 'center', behavior: 'instant' });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [findOpen, findMatchId, findNavigation, state.historyLoading]);

  useEffect(() => {
    if (state.ready && !hydrated.current) {
      hydrated.current = true;
      setDrafts(state.drafts || {});
    }
    if (state.ready && !readySent.current) { readySent.current = true; dispatch('ready'); }
  }, [state.ready, state.drafts, dispatch]);

  const onNew = useCallback(async (message: AppendMessage) => {
    const text = message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n').trim();
    if (!text || state.busy || state.historyLoading || pending) return;
    const record = { conversationId: state.conversationId, text };
    setPending(record);
    nearBottom.current = true;
    await new Promise<void>((resolve) => {
      dispatch('send', { text, conversationId: state.conversationId }, (result) => {
        if (result.ok) setDrafts((value) => value[record.conversationId]?.trim() === text ? { ...value, [record.conversationId]: '' } : value);
        setPending(null);
        resolve();
      });
    });
  }, [state.busy, state.historyLoading, state.conversationId, state.messages, pending, dispatch]);

  const runtimeMessages = useMemo(() => state.messages.filter((item) => item.role === 'user' || item.role === 'assistant').map((item): ThreadMessageLike => ({
    id: String(item.id), role: item.role,
    content: [{ type: 'text', text: String(item.content ?? '') }],
    metadata: { custom: { source: item.source, screen_event: item.screen_event, request_status: item.metadata?.request_status,
      selection_context: item.metadata?.selection_context,
      retryAvailable: item.retryAvailable, retryStatus: item.retryStatus } },
  })), [state.messages]);

  const runtime = useExternalStoreRuntime({
    messages: runtimeMessages,
    convertMessage: (message) => message,
    isRunning: state.busy && state.requestInCurrent,
    onNew,
  });

  useEffect(() => {
    if (!pending) return;
    if (state.error && !state.busy) {
      setPending(null);
    }
  }, [state, pending]);

  useEffect(() => {
    if (!searchStarted.current) { searchStarted.current = true; return; }
    const timeout = setTimeout(() => dispatch('search', { query: search }), 250);
    return () => clearTimeout(timeout);
  }, [search, dispatch]);

  useLayoutEffect(() => {
    if (editor.current) {
      editor.current.style.height = 'auto';
      editor.current.style.height = `${Math.min(editor.current.scrollHeight, 160)}px`;
    }
  }, [draft]);

  useLayoutEffect(() => {
    const node = viewport.current;
    if (!node) return;
    if (previousConversation.current !== state.conversationId || nearBottom.current) {
      node.scrollTop = node.scrollHeight;
      nearBottom.current = true;
      setShowLatest(false);
    }
    previousConversation.current = state.conversationId;
  }, [state.messages, state.conversationId, state.historyLoading]);

  useEffect(() => {
    const observer = new ResizeObserver(() => {
      if (nearBottom.current && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    });
    if (readingColumn.current) observer.observe(readingColumn.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setComposerHeight(entry.borderBoxSize[0]?.blockSize || entry.contentRect.height));
    if (composerArea.current) observer.observe(composerArea.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    return () => {
      clearTimeout(focusHighlightTimer.current);
      focusHighlightNode.current?.classList.remove('message-highlight');
    };
  }, []);

  useEffect(() => {
    if (!state.focusRequest?.id) return;
    const request = state.focusRequest;
    const key = `${request.serial}:${request.id}`;
    if (appliedFocus.current === key) return;
    const frame = requestAnimationFrame(() => {
      const node = viewport.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(request.id)}"]`);
      if (node) {
        clearTimeout(focusHighlightTimer.current);
        focusHighlightNode.current?.classList.remove('message-highlight');
        focusHighlightNode.current = node;
        appliedFocus.current = key;
        nearBottom.current = false;
        node.scrollIntoView({ behavior: 'smooth', block: 'center' });
        node.classList.add('message-highlight');
        focusHighlightTimer.current = setTimeout(() => {
          node.classList.remove('message-highlight');
          if (focusHighlightNode.current === node) focusHighlightNode.current = null;
        }, 2200);
      }
    });
    // A host snapshot may replace the messages array without changing this focus
    // request. Its cleanup only cancels a pending lookup; the highlight owns its
    // timer independently, while appliedFocus keeps scrolling one-shot.
    return () => cancelAnimationFrame(frame);
  }, [state.focusRequest?.id, state.focusRequest?.serial, state.messages]);

  const openSearch = useCallback(() => {
    setSearching(true);
    setSidebarOpen(true);
    setDesktopSidebar(true);
    setTimeout(() => { searchInput.current?.focus(); searchInput.current?.select(); }, 30);
  }, []);

  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)');
    const changed = () => setNarrow(media.matches);
    media.addEventListener('change', changed);
    return () => media.removeEventListener('change', changed);
  }, []);

  useLayoutEffect(() => {
    if (!sidebarOpen || !narrow || !sidebar.current) return;
    drawerOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = sidebar.current;
    const background = main.current;
    if (background) background.inert = true;
    const frame = requestAnimationFrame(() => {
      (panel.querySelector<HTMLInputElement>('input') || panel.querySelector<HTMLButtonElement>('[aria-current="page"]')
        || panel.querySelector<HTMLButtonElement>('button:not(:disabled)'))?.focus();
    });
    const trap = (event: globalThis.KeyboardEvent) => {
      if (document.querySelector('dialog[open]')) return;
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation();
        setSidebarOpen(false); setRowMenu(null); return;
      }
      if (event.key !== 'Tab') return;
      const items = [...panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href]'),
        ...document.querySelectorAll<HTMLElement>('.row-popover button:not(:disabled)')].filter((item) => item.getClientRects().length > 0);
      const current = items.indexOf(document.activeElement as HTMLElement);
      if (!items.length) { event.preventDefault(); return; }
      if (current < 0 || event.shiftKey && current === 0 || !event.shiftKey && current === items.length - 1) {
        event.preventDefault(); (event.shiftKey ? items[items.length - 1] : items[0]).focus();
      }
    };
    document.addEventListener('keydown', trap, true);
    return () => {
      cancelAnimationFrame(frame); document.removeEventListener('keydown', trap, true);
      if (background) background.inert = false;
      const origin = drawerOrigin.current;
      if (origin?.isConnected) origin.focus();
    };
  }, [sidebarOpen, narrow]);

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if (modal || document.querySelector('dialog[open]')) {
        if ((event.ctrlKey || event.metaKey) && ['n', 'f'].includes(event.key.toLowerCase())) event.preventDefault();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        if (!state.historyLoading && state.ready) dispatch('new');
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f' && event.shiftKey) {
        event.preventDefault(); if (state.ready && !state.historyLoading) openFind();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault(); openSearch();
      } else if (event.key === 'F3' && findOpen) {
        event.preventDefault(); moveFind(event.shiftKey ? -1 : 1);
      } else if (event.key === 'Escape' && findOpen) {
        event.preventDefault(); closeFind();
      } else if (event.key === 'Escape' && !modal) {
        setToolsOpen(false); setRowMenu(null); setSidebarOpen(false);
        if (searching) { setSearching(false); setSearch(''); }
        editor.current?.focus();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [state.historyLoading, state.ready, dispatch, openSearch, modal, searching, openFind, closeFind, moveFind, findOpen]);

  useEffect(() => {
    if (!toolsOpen && !rowMenu) return;
    const close = (event: PointerEvent) => {
      if (!(event.target as HTMLElement).closest('[data-popover]')) { setToolsOpen(false); setRowMenu(null); }
    };
    window.addEventListener('pointerdown', close);
    return () => window.removeEventListener('pointerdown', close);
  }, [toolsOpen, rowMenu]);

  useEffect(() => {
    if (!toolsOpen && !rowMenu) return;
    const frame = requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(rowMenu ? '.row-popover button:not(:disabled)' : '.tools-popover button:not(:disabled)')?.focus());
    const navigate = (event: globalThis.KeyboardEvent) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const menu = document.querySelector(rowMenu ? '.row-popover' : '.tools-popover');
      if (!menu?.contains(document.activeElement)) return;
      const buttons = [...menu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      event.preventDefault(); buttons[next]?.focus();
    };
    window.addEventListener('keydown', navigate);
    return () => { cancelAnimationFrame(frame); window.removeEventListener('keydown', navigate); };
  }, [toolsOpen, rowMenu]);

  const send = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend || composition.current) return;
    runtime.thread.append({ role: 'user', content: [{ type: 'text', text: draft.trim() }] });
    editor.current?.focus();
  };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229 && !composition.current) {
      event.preventDefault();
      if (!event.repeat) send();
    }
  };
  const showModal = (value: ModalState) => {
    setModal(value); setToolsOpen(false); setRowMenu(null); setSidebarOpen(false);
    renameComposition.current = false;
    if (value?.kind === 'rename') setRenameValue(value.title);
  };
  const closeModal = () => {
    setModal(null);
    // A modal dialog rejects background focus until React removes it.
    requestAnimationFrame(() => editor.current?.focus());
  };
  const goLatest = () => { viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: 'smooth' }); nearBottom.current = true; setShowLatest(false); };

  const recoveryLabel = ({ settings: '打开 API 设置', history: '重新读取', retry: '重试回复' } as Record<string, string>)[state.errorAction];
  const recover = () => {
    if (state.errorAction === 'settings') dispatch('settings');
    else if (state.errorAction === 'history') dispatch('reloadHistory', { conversationId: state.errorConversationId });
    else if (state.errorAction === 'retry') dispatch('retry', { conversationId: state.errorConversationId, messageId: state.errorMessageId });
  };

  return <AssistantRuntimeProvider runtime={runtime}><HostContext.Provider value={{ dispatch, conversationId: state.conversationId, busy: state.busy, focusRequest: state.focusRequest, findMatches, findMatchId }}>
    <div className={`chat-shell ${desktopSidebar ? '' : 'sidebar-collapsed'} ${sidebarOpen ? 'sidebar-open' : ''}`}
      style={{ '--chat-font-size': `${state.chatFontSize || 16}px` } as CSSProperties}>
      <a className="skip-link" href="#conversation">跳到对话</a>
      {sidebarOpen && <button className="sidebar-backdrop" tabIndex={-1} aria-label="关闭历史会话" onClick={() => setSidebarOpen(false)} />}
      <aside ref={sidebar} className="sidebar" data-testid="sidebar" aria-label="历史会话" role={sidebarOpen && narrow ? 'dialog' : undefined} aria-modal={sidebarOpen && narrow || undefined}>
        <div className="sidebar-top">
          <span className="wordmark">AURA<span className="wordmark-dot" /></span>
          <div className="sidebar-controls"><IconButton label="搜索会话 · Ctrl+F" onClick={openSearch}><Search size={17} /></IconButton><IconButton className="close-sidebar" label="收起历史会话" onClick={() => { setSidebarOpen(false); setDesktopSidebar(false); }}><PanelLeftClose size={18} /></IconButton></div>
        </div>
        <button type="button" className="new-conversation" disabled={!state.ready || state.historyLoading} onClick={() => { dispatch('new'); setSidebarOpen(false); }}><Plus size={17} /><span>新对话</span><kbd>Ctrl N</kbd></button>
        {searching && <div className="search-field"><Search size={15} /><input data-testid="conversation-search" ref={searchInput} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索对话" aria-label="搜索历史会话" /><IconButton label="关闭搜索" onClick={() => { setSearching(false); setSearch(''); }}><X size={14} /></IconButton></div>}
        <nav className="conversation-list" aria-label="会话列表">
          {state.conversations.map((item) => <div key={item.id} data-conversation-id={item.id} className={`conversation-row ${item.id === state.conversationId ? 'selected' : ''} ${rowMenu === item.id ? 'menu-active' : ''}`}>
            <button type="button" className="conversation-link" title={item.title || '新对话'} aria-current={item.id === state.conversationId ? 'page' : undefined} disabled={state.historyLoading} onClick={() => { dispatch('open', { id: item.id }); setSidebarOpen(false); setRowMenu(null); }}>
              <span className="conversation-title">{item.title || '新对话'}</span>
            </button>
            <div className="row-actions" data-popover><IconButton label={`管理会话：${item.title || '新对话'}`} onClick={(event) => {
              const bounds = event.currentTarget.getBoundingClientRect();
              setRowMenuPosition({ left: Math.max(8, Math.min(window.innerWidth - 162, bounds.right - 154)), top: Math.max(8, Math.min(window.innerHeight - 96, bounds.bottom + 4)) });
              setRowMenu(rowMenu === item.id ? null : item.id);
            }} aria-expanded={rowMenu === item.id}><MoreHorizontal size={16} /></IconButton>
              {rowMenu === item.id && createPortal(<div className="popover row-popover" data-popover role="menu" style={rowMenuPosition}><button role="menuitem" onClick={() => showModal({ kind: 'rename', id: item.id, title: item.title })} disabled={state.historyLoading}><Pencil size={15} />重命名</button><button role="menuitem" className="danger" onClick={() => { dispatch('delete', { id: item.id }); setRowMenu(null); }} disabled={state.busy || state.historyLoading}><Trash2 size={15} />删除会话</button></div>, document.body)}
            </div>
          </div>)}
          {state.ready && !state.conversations.length && <p className="list-empty">{search ? '没有找到相关对话' : '新对话会保存在这里'}</p>}
        </nav>
        <div className="sidebar-bottom">{state.canSettings && <button className="sidebar-setting" onClick={() => dispatch('settings')}><Settings2 size={16} /><span>设置</span></button>}<span className="local-note">对话保存在本机</span></div>
      </aside>

      <main ref={main} className="main-panel" id="conversation">
        <header className="chat-header">
          <IconButton data-testid="history-toggle" className="show-sidebar" label="展开历史会话" onClick={() => { setSidebarOpen(true); setDesktopSidebar(true); }}><Menu size={20} /></IconButton>
          <div className="header-title" title={state.title || 'AURA'}>{state.title || 'AURA'}</div>
          <div className="header-actions" data-popover><IconButton label="新对话 · Ctrl+N" disabled={!state.ready || state.historyLoading} onClick={() => dispatch('new')}><Plus size={19} /></IconButton><IconButton data-testid="tools-button" label="对话工具" onClick={() => setToolsOpen((value) => !value)} aria-expanded={toolsOpen}><MoreHorizontal size={21} /></IconButton>
            {toolsOpen && <div className="popover tools-popover" data-testid="tools-menu" role="menu">
              <button role="menuitem" disabled={!state.ready || state.historyLoading} onClick={openFind}><Search size={16} />查找当前对话<kbd>Ctrl⇧F</kbd></button>
              {state.canSettings && <button role="menuitem" onClick={() => { dispatch('settings'); setToolsOpen(false); }}><Settings2 size={16} />聊天服务设置</button>}
              {state.canCamera && <button role="menuitem" onClick={() => { dispatch('cameraSettings'); setToolsOpen(false); }}><Camera size={16} />摄像头识别</button>}
              {state.canMemory && <button role="menuitem" onClick={() => { dispatch('memory'); setToolsOpen(false); }}><BookOpen size={16} />长期记忆</button>}
              {state.perception && <button role="menuitem" onClick={() => { dispatch('openPerceptionSettings'); setToolsOpen(false); }}><Settings2 size={16} />屏幕感知设置</button>}
              {state.canBudget && <button role="menuitem" onClick={() => { dispatch('budget'); setToolsOpen(false); }}><SlidersHorizontal size={16} />上下文预算</button>}
              <div className="reading-controls" role="group" aria-label="聊天阅读字号">
                <span>阅读字号</span><button type="button" aria-label="减小聊天字号" disabled={(state.chatFontSize || 16) <= 12}
                  onClick={() => dispatch('setChatFontSize', { size: (state.chatFontSize || 16) - 1 })}>A−</button>
                <output aria-live="polite">{state.chatFontSize || 16}</output>
                <button type="button" aria-label="增大聊天字号" disabled={(state.chatFontSize || 16) >= 22}
                  onClick={() => dispatch('setChatFontSize', { size: (state.chatFontSize || 16) + 1 })}>A＋</button>
                <button type="button" className="reading-reset" onClick={() => dispatch('setChatFontSize', { size: 16 })}>重置</button>
              </div>
              {state.canExport && <button role="menuitem" disabled={!state.messages.length || state.busy || state.historyLoading || state.exportBusy} onClick={() => { dispatch('export', { conversationId: state.conversationId }); setToolsOpen(false); }}>{state.exportBusy ? <LoaderCircle size={16} className="spin" /> : <Download size={16} />}导出当前对话</button>}
              {state.contextUsage && <p className="context-note">{state.contextUsage}</p>}
              <div className="menu-divider" /><button role="menuitem" className="danger" disabled={!state.messages.length || state.busy || state.historyLoading} onClick={() => { dispatch('clear', { conversationId: state.conversationId }); setToolsOpen(false); }}><Trash2 size={16} />清空当前对话</button>
            </div>}
          </div>
        </header>
        {findOpen && <div className="message-find" role="search" aria-label="查找当前对话">
          <Search size={15} /><input ref={findInput} data-testid="message-search" aria-label="在当前对话中查找" placeholder="查找当前对话…" value={findQuery}
            onChange={(event) => { setFindQuery(event.target.value); setFindPosition(0); }} onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
              if (event.key === 'Enter') { event.preventDefault(); moveFind(event.shiftKey ? -1 : 1); }
            }} />
          <output className="message-find-count" aria-live="polite">{!findQuery.trim() ? '输入关键词' : findIds.length ? `${findIndex + 1} / ${findIds.length}` : '无匹配'}</output>
          <IconButton label="上一条匹配消息" disabled={!findIds.length} onClick={() => moveFind(-1)}><ChevronUp size={17} /></IconButton>
          <IconButton label="下一条匹配消息" disabled={!findIds.length} onClick={() => moveFind(1)}><ChevronDown size={17} /></IconButton>
          <IconButton label="关闭对话内查找" onClick={closeFind}><X size={16} /></IconButton>
        </div>}
        {state.perception && <div className="perception-status" role="status">
          <button type="button" onClick={() => dispatch('openPerceptionSettings')} title={state.perception.status}>
            {state.perception.permissionSaveFailed ? '屏幕关闭状态未保存' : state.perception.paused ? '屏幕感知已暂停' : state.perception.analysisError ? '屏幕分析失败 · 查看原因' : state.perception.enabled ? state.perception.allowRemote ? '屏幕感知运行中' : '屏幕仅本地观察' : '屏幕感知未启用'}
          </button>
          {state.perception.enabled && !state.perception.paused && <span>发言冷却 {state.perception.cooldownSeconds} 秒 · 本小时还可评论 {state.perception.hourlyRemaining} 次</span>}
          <button type="button" onClick={() => dispatch('setPerceptionPaused', { paused: !state.perception!.paused })}>
            {state.perception.paused ? '恢复屏幕感知' : '暂停屏幕感知'}</button>
        </div>}
        {state.camera && <div className="perception-status camera-runtime" role="status">
          <button type="button" onClick={() => dispatch('cameraSettings')} title={state.camera.status}>
            <Camera size={13} />{state.camera.permissionSaveFailed ? '摄像头关闭状态未保存' : state.camera.paused ? '摄像头已暂停' : state.camera.cameraError ? '摄像头设备异常 · 查看原因' : state.camera.analysisError ? '摄像头分析失败 · 查看原因' : state.camera.enabled ? state.camera.allowRemote ? '摄像头允许远程分析' : '摄像头仅本地预览' : '摄像头已关闭'}
          </button>
          {state.camera.enabled && <button type="button" onClick={() => dispatch('setCameraPaused', { paused: !state.camera!.paused })}>
            {state.camera.paused ? '恢复摄像头' : '暂停摄像头'}</button>}
          {(state.camera.enabled || state.perception?.enabled) && <button type="button" className="pause-all"
            disabled={(state.camera.paused || !state.camera.enabled) && (state.perception?.paused || !state.perception?.enabled)}
            onClick={() => dispatch('pauseAllVisual')} title="仅暂停本次运行；恢复时请分别选择屏幕或摄像头">暂停全部观察</button>}
        </div>}

        <ThreadPrimitive.Root className="thread">
          <div ref={viewport} className="thread-viewport" onScroll={() => {
            const node = viewport.current!;
            nearBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 72;
            setShowLatest(!nearBottom.current);
          }}>
            <div className="reading-column" ref={readingColumn}>
              {!state.ready ? <div className="empty-state"><div className="aura-mark"><span /></div>{connectionError ? <><h1>暂时无法连接</h1><p>{connectionError}</p></> : <><h1>正在连接 AURA</h1><p className="loading-text"><LoaderCircle size={16} className="spin" />正在读取你的对话</p></>}</div> : state.historyLoading ? <div className="history-loading" role="status"><LoaderCircle size={19} className="spin" />正在读取对话…</div> : !state.messages.length ? <div className="empty-state"><div className="aura-mark"><span /></div><h1>今天想聊些什么？</h1><p>一个问题，一个想法，都可以从这里开始。</p>{state.canSettings && <button className="empty-settings" onClick={() => dispatch('settings')}>配置聊天服务<ChevronRight size={14} /></button>}</div> : <ThreadPrimitive.Messages components={{ UserMessage: ChatMessageView, AssistantMessage: ChatMessageView }} />}
            </div>
          </div>
          {showLatest && <button className="latest-button" style={{ bottom: composerHeight + 12 }} type="button" onClick={goLatest}><ArrowDown size={16} />回到最新</button>}
          <div className="composer-area" ref={composerArea}>
            <div className="composer-column">
              {(state.error || connectionError) && state.ready && <div className="error-banner" role="alert"><span>{state.error || connectionError}{state.errorRetryStatus && <small className="retry-explanation">{state.errorRetryStatus}</small>}</span>{state.error && recoveryLabel && <button type="button" disabled={state.busy || state.historyLoading || (state.errorAction === 'retry' && state.errorRetryAvailable === false)} onClick={recover}>{recoveryLabel}</button>}</div>}
              {state.exportStatus && <p className="export-status" role="status">{state.exportStatus}</p>}
              <form className={`composer ${state.busy ? 'composer-busy' : ''}`} onSubmit={send}>
                <label htmlFor="message-input" className="sr-only">发送给 AURA 的消息</label>
                <textarea id="message-input" data-testid="message-input" ref={editor} rows={1} value={draft} placeholder={state.historyLoading ? '正在读取对话…' : '发送消息…'} disabled={!state.ready || state.historyLoading} onChange={(event) => {
                  const text = event.target.value;
                  setDrafts((value) => ({ ...value, [state.conversationId]: text }));
                  dispatch('draft', { conversationId: state.conversationId, text });
                }} onKeyDown={keyDown} onCompositionStart={() => { composition.current = true; }} onCompositionEnd={() => { composition.current = false; }} />
                <div className="composer-toolbar"><div className="composer-state" role="status">{(state.busy || pending) ? <><LoaderCircle size={13} className="spin" /><span>{state.status || '正在回复…'}</span></> : <span className="keyboard-hint">Shift + Enter 换行</span>}</div><div className="composer-actions">{state.canStop && <button type="button" data-testid="stop-button" className="stop-button" onClick={() => dispatch('stop', { conversationId: state.requestConversationId })} title="停止等待本次回复；服务端可能已收到请求"><Square size={12} />停止生成</button>}<button data-testid="send-button" className="send-button" type="submit" disabled={!canSend} aria-label={state.busy ? '正在回复，请稍候' : '发送消息'} title="发送消息 · Enter"><ArrowUp size={19} strokeWidth={2.4} /></button></div></div>
              </form>
              <div className="composer-footnote"><span>AURA</span><span>Enter 发送</span></div>
            </div>
          </div>
        </ThreadPrimitive.Root>
      </main>

      {modal && <Modal title="重命名会话" onClose={closeModal}>
        <form onSubmit={(event) => {
          event.preventDefault();
          if (!renameValue.trim()) return; dispatch('rename', { id: modal.id, title: renameValue.trim() });
          closeModal();
        }}>
          <label className="dialog-label" htmlFor="rename-title">会话名称</label><input autoFocus id="rename-title" className="dialog-input" value={renameValue} maxLength={200} onChange={(event) => setRenameValue(event.target.value)}
            onCompositionStart={() => { renameComposition.current = true; }} onCompositionEnd={() => { renameComposition.current = false; }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (renameComposition.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)) event.preventDefault();
            }} />
          <div className="dialog-actions"><button type="button" className="secondary-button" onClick={closeModal}>取消</button><button type="submit" className="primary-button" disabled={!renameValue.trim()}>保存</button></div>
        </form>
      </Modal>}
    </div>
  </HostContext.Provider></AssistantRuntimeProvider>;
}
