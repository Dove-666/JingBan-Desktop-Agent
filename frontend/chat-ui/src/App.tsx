import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ClipboardEvent as ReactClipboardEvent, CSSProperties, DragEvent as ReactDragEvent, FormEvent, KeyboardEvent, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  AssistantRuntimeProvider, MessagePrimitive, ThreadPrimitive,
  useExternalStoreRuntime, useAuiState,
} from '@assistant-ui/react';
import type { AppendMessage, ThreadMessageLike } from '@assistant-ui/react';
import {
  ArrowDown, ArrowUp, Check, ChevronDown, ChevronRight, ChevronUp, Copy,
  Download, LoaderCircle, Menu, MoreHorizontal, PanelLeftClose,
  Pencil, Plus, Search, Settings2, Square, Trash2, X, Gamepad2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import whaleAvatar from '../../../assets/expressions/neutral.png';
import { IMAGE_THUMBNAIL_EVENT, normalizeImageRevision, normalizeImageThumbnail, normalizeThumbnailError, useBridge } from './bridge';
import { AgentInteractionCard } from './components/AgentInteractionCard';
import { GlassSurface } from './components/GlassSurface';
import { displayedReply, ExecutionDetails } from './components/ExecutionDetails';
import { AgentArtifactCards } from './components/AgentArtifactCards';
import { ConversationRail } from './components/ConversationRail';
import { hasFileTransfer, ImageInputController } from './imageInput';
import type { ImageInputContext, ImageInputSnapshot } from './imageInput';
import type { ChatMessage, ChatState, Dispatch, ExecutionTrace, ImageErrorCode, ImageThumbnail, ImageThumbnailReady, ScreenEvent, SelectionSource } from './bridge';

const IMAGE_ERROR_TEXT: Record<ImageErrorCode, string> = {
  image_missing: '引用的图片不存在或已删除，请重新发送图片后重试。',
  image_unavailable: '无法读取或校验引用的图片，请重新发送图片后重试。',
  vision_rejected: '图文请求未被模型接口接受，请确认模型支持图片并检查接口配置。',
};

type ModalState = { kind: 'rename'; id: string; title: string } | null;
type PendingSend = { conversationId: string; text: string; draftSequence: number };
type ConversationAction = 'find' | 'export' | 'clear';
type PendingConversationAction = {
  conversationId: string; kind: ConversationAction; serial: number; accepted: boolean;
};
type ReadingPosition = { top: number; anchorId?: string; offset: number; following: boolean };
type ScrollGeometry = { conversationId: string; top: number; height: number; client: number };

function captureReadingPosition(node: HTMLElement, following: boolean): ReadingPosition {
  const bounds = node.getBoundingClientRect();
  const messages = node.querySelectorAll<HTMLElement>('[data-message-id]');
  // Message rectangles are ordered vertically; avoid a layout read for every
  // message on each scroll in long histories.
  let start = 0, end = messages.length;
  while (start < end) {
    const middle = (start + end) >>> 1;
    if (messages[middle].getBoundingClientRect().bottom <= bounds.top + 4) start = middle + 1;
    else end = middle;
  }
  const anchor = messages[start];
  return { top: node.scrollTop, anchorId: anchor?.dataset.messageId,
    offset: anchor ? anchor.getBoundingClientRect().top - bounds.top : 0, following };
}

function restoreReadingPosition(node: HTMLElement, saved: ReadingPosition) {
  const anchor = saved.anchorId
    ? node.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(saved.anchorId)}"]`) : null;
  if (anchor) node.scrollTop += anchor.getBoundingClientRect().top - node.getBoundingClientRect().top - saved.offset;
  else node.scrollTop = saved.top;
}
type DisplayedErrorBinding = { conversationId: string; messageId: string; error: string; retryStatus: string };
const HostContext = createContext<{ dispatch: Dispatch; conversationId: string; busy: boolean; canBudget?: boolean;
  focusRequest?: { id: string; serial: number }; findMatches?: Set<string>; findMatchId?: string;
  displayedError?: DisplayedErrorBinding }>({ dispatch: () => false, conversationId: '', busy: false });

function guidanceShownGlobally(text: string | undefined, messageId: string, conversationId: string,
  error: DisplayedErrorBinding | undefined) {
  return Boolean(text && error && error.conversationId === conversationId && error.messageId === messageId
    && (text === error.error || text === error.retryStatus));
}
// A changing converter invalidates assistant-ui's entire message cache. Draft
// edits should update the editor, not reconvert all previous messages.
const identityMessage = (message: ThreadMessageLike) => message;
const EMPTY_FIND_RESULTS: readonly string[] = [];

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

function MessageImageAttachment({ conversationId, messageId, imageId, imageRevision, thumbnail, error, unavailable }: {
  conversationId: string; messageId: string; imageId: string; imageRevision: number; thumbnail?: ImageThumbnail; error?: string; unavailable?: boolean;
}) {
  const { dispatch } = useContext(HostContext);
  const container = useRef<HTMLDivElement>(null);
  const requested = useRef(false);
  const visibleNow = useRef(false);
  const cycle = useRef(0);
  const [visible, setVisible] = useState(false);
  const [preview, setPreview] = useState<ImageThumbnail>();
  const [size, setSize] = useState(thumbnail ? { width: thumbnail.width, height: thumbnail.height } : undefined);
  const [loadError, setLoadError] = useState<string>();
  const [failed, setFailed] = useState(false);
  const accept = useCallback((result: ImageThumbnail) => {
    setPreview(result);
    setSize((previous) => previous?.width === result.width && previous?.height === result.height
      ? previous : { width: result.width, height: result.height });
  }, []);
  useEffect(() => {
    const node = container.current;
    if (!node || unavailable) {
      visibleNow.current = false;
      requested.current = false;
      cycle.current++;
      setPreview(undefined);
      setVisible(false);
      return;
    }
    const update = (next: boolean) => {
      if (next === visibleNow.current) return;
      visibleNow.current = next;
      if (!next) {
        cycle.current++;
        requested.current = false;
        setPreview(undefined);
      }
      setVisible(next);
    };
    const viewport = node.closest('.thread-viewport');
    if (typeof IntersectionObserver === 'undefined') {
      let frame = 0;
      const measure = () => {
        const bounds = node.getBoundingClientRect();
        const root = viewport?.getBoundingClientRect();
        update(bounds.bottom >= (root?.top ?? 0) - 240 && bounds.top <= (root?.bottom ?? window.innerHeight) + 240);
      };
      const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure); };
      measure(); viewport?.addEventListener('scroll', schedule, { passive: true });
      window.addEventListener('resize', schedule);
      return () => { cancelAnimationFrame(frame); viewport?.removeEventListener('scroll', schedule); window.removeEventListener('resize', schedule); };
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => update(entry.isIntersecting));
    }, { root: viewport, rootMargin: '240px 0px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [unavailable]);
  useEffect(() => {
    if (error) setLoadError(error);
    if (visible && thumbnail && !error && !unavailable && !failed) accept(thumbnail);
  }, [visible, thumbnail, error, unavailable, failed, accept]);
  useEffect(() => {
    const receive = (event: Event) => {
      const result = (event as CustomEvent<ImageThumbnailReady>).detail;
      if (!visibleNow.current || unavailable || !result || result.conversationId !== conversationId ||
          result.messageId !== messageId || result.imageId !== imageId ||
          normalizeImageRevision(result.imageRevision) !== imageRevision) return;
      const safeError = normalizeThumbnailError(result.error);
      const safePreview = normalizeImageThumbnail(result.thumbnail);
      if (safeError) setLoadError(safeError);
      else if (safePreview) accept(safePreview);
      else setFailed(true);
    };
    window.addEventListener(IMAGE_THUMBNAIL_EVENT, receive);
    return () => window.removeEventListener(IMAGE_THUMBNAIL_EVENT, receive);
  }, [conversationId, messageId, imageId, imageRevision, unavailable, accept]);
  useEffect(() => {
    if (!visible || preview || thumbnail || error || loadError || failed || unavailable || requested.current) return;
    requested.current = true;
    const originCycle = cycle.current;
    let active = true;
    dispatch('loadImageThumbnail', { conversationId, messageId }, (result) => {
      if (active && visibleNow.current && originCycle === cycle.current && !result.ok) setFailed(true);
    });
    return () => { active = false; };
  }, [visible, preview, thumbnail, error, loadError, failed, unavailable, dispatch, conversationId, messageId]);
  if (unavailable) return null;
  const open = () => dispatch('openImage', { conversationId, messageId });
  const displayScale = size ? Math.min(1, 360 / size.width, 360 / size.height) : 1;
  const dimensions = size ? { width: Math.round(size.width * displayScale), aspectRatio: `${size.width} / ${size.height}` }
    : { width: 320, aspectRatio: '16 / 9' };
  const failure = error || loadError;
  return <div ref={container} className="image-attachment" data-testid="image-attachment" data-near-viewport={visible}>
    {visible && preview && !failure && !failed ? <button type="button" className="image-thumbnail-button"
      style={{ width: dimensions.width }} aria-label="查看图片" title="点击查看大图" onClick={open}>
      <img data-testid="image-thumbnail" src={preview.src} width={preview.width} height={preview.height}
        alt="消息中的图片" draggable={false} loading="lazy" onError={() => setFailed(true)} />
      <span className="image-thumbnail-caption">点击查看大图</span>
    </button> : failure || failed ? <div className="image-thumbnail-fallback" role="status" data-testid="image-thumbnail-error">
      <span>{failure || '图片预览暂不可用。'}</span>
      {!failure && <button type="button" className="image-attachment-open" onClick={open}>查看图片</button>}
    </div> : <div className="image-thumbnail-reserved" style={{ width: dimensions.width }} aria-hidden={!visible}>
      <div className="image-thumbnail-placeholder" style={{ aspectRatio: dimensions.aspectRatio }}
        role={visible ? 'status' : undefined} data-testid="image-thumbnail-loading">{visible ? '正在加载图片…' : ''}</div>
      <span className="image-thumbnail-caption" aria-hidden="true">点击查看大图</span>
    </div>}
  </div>;
}

function ChatMessageView() {
  const message = useAuiState((value) => value.message);
  const { dispatch, focusRequest, conversationId, busy, findMatches, findMatchId, displayedError } = useContext(HostContext);
  const rawText = message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n');
  const meta = message.metadata.custom as { source?: string; screen_event?: ScreenEvent; selection_context?: SelectionSource; request_status?: string; retryAvailable?: boolean; retryStatus?: string;
    agent_error_code?: string; agent_error?: string;
    image_id?: string; image_revision?: number; image_error?: ImageErrorCode; image_unavailable?: boolean;
    image_thumbnail?: ImageThumbnail; image_thumbnail_error?: string; execution?: ExecutionTrace;
    streaming_status?: 'streaming' | 'interrupted' };
  const isUser = message.role === 'user';
  const text = isUser ? rawText : displayedReply(rawText, meta.execution);
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
  return <MessagePrimitive.Root className={`message ${isUser ? 'message-user' : 'message-assistant'} ${findMatches?.has(message.id) ? 'message-find-match' : ''} ${findMatchId === message.id ? 'message-find-current' : ''}`} data-message-id={message.id}
    data-stream-state={meta.streaming_status} aria-busy={meta.streaming_status === 'streaming' || undefined}>
    {!isUser && <span className="assistant-avatar"><img src={whaleAvatar} alt="鲸鱼娘" draggable={false} /></span>}
    <div className="message-body">
      {!isUser && meta.execution && <ExecutionDetails execution={meta.execution} />}
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
      {isUser && meta.image_id && /^[0-9a-f]{64}$/.test(meta.image_id) &&
        <MessageImageAttachment key={`${conversationId}:${message.id}:${meta.image_id}:${meta.image_revision ?? 0}`}
          conversationId={conversationId} messageId={message.id} imageId={meta.image_id} imageRevision={meta.image_revision ?? 0} thumbnail={meta.image_thumbnail}
          error={meta.image_thumbnail_error} unavailable={meta.image_unavailable} />}
      <div className="markdown"><Markdown text={text} /></div>
      {!isUser && meta.streaming_status === 'streaming' && <span className="streaming-indicator" role="status" aria-label="正在生成回复"><LoaderCircle size={13} className="spin" aria-hidden="true" /></span>}
      {!isUser && meta.streaming_status === 'interrupted' && <span className="request-message-state" role="status">部分回复 · 未完成</span>}
      {isUser && meta.image_unavailable &&
        <p className="image-attachment-error" role="status" data-testid="image-attachment-unavailable">图片附件不可用，请重新发送图片。</p>}
      {isUser && meta.request_status === 'failed' && meta.image_error && IMAGE_ERROR_TEXT[meta.image_error] &&
        <p className="image-attachment-error" role="status" data-testid="image-attachment-error">{IMAGE_ERROR_TEXT[meta.image_error]}</p>}
      {isUser && meta.selection_context && <details className="selection-source" data-testid="selection-source">
        <summary>引用选区 · {meta.selection_context.title || '当前页面'}</summary>
        <p>{meta.selection_context.text}</p>
        {meta.selection_context.url && <ExternalLink href={meta.selection_context.url}>查看来源页面</ExternalLink>}
        <small>{meta.selection_context.page_characters ? `已附带 ${meta.selection_context.page_characters} 字页面参考，模型请求按预算截取。` : '本次只附带所选文字。'}</small>
        {meta.selection_context.notes?.map((note, index) => <small key={index}>{note}</small>)}
      </details>}
      {isUser && ['failed', 'cancelled', 'interrupted', 'pending'].includes(meta?.request_status || '') && !(meta.execution && meta.request_status === 'pending') &&
        <div className={`request-message-state request-${meta.request_status}`} role="status" data-testid="message-request-state">
          <span>{meta.request_status === 'failed' && meta.agent_error_code ?
            (meta.agent_error_code === 'timeout' ? '任务超时' : '任务未完成') :
            ({ failed: '回复失败', cancelled: '已停止生成', interrupted: '上次请求未完成', pending: '正在等待回复' } as Record<string, string>)[meta.request_status!]}</span>
          {meta.agent_error && !guidanceShownGlobally(meta.agent_error, message.id, conversationId, displayedError)
            && <span className="retry-explanation" data-testid="agent-message-error">{meta.agent_error}</span>}
          {meta.request_status !== 'pending' && meta.retryAvailable !== false && <button type="button" disabled={busy} onClick={() =>
            dispatch('retry', { conversationId, messageId: message.id })}>重试回复</button>}
          {meta.retryStatus && !guidanceShownGlobally(meta.retryStatus, message.id, conversationId, displayedError)
            && <span className="retry-explanation">{meta.retryStatus}</span>}
        </div>}
      {meta.streaming_status !== 'streaming' && <div className="message-actions">
        <IconButton label={copied ? '已复制' : '复制消息'} onClick={copy}>{copied ? <Check size={15} /> : <Copy size={15} />}</IconButton>
      </div>}
    </div>
    {isUser && meta.execution && <div className="execution-user-result">
      <span className="assistant-avatar"><img src={whaleAvatar} alt="鲸鱼娘" draggable={false} /></span>
      <ExecutionDetails execution={meta.execution} />
    </div>}
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
  // Change the material in place: preserve the editor, draft and scroll state.
  // Depending only on theme also keeps ordinary keystrokes off the root style.
  useLayoutEffect(() => {
    // Native installs the saved theme before hydration. Keep that bootstrap
    // material until the first authoritative bridge snapshot arrives.
    if (!state.ready) return;
    const theme = state.theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }, [state.ready, state.theme]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [desktopSidebar, setDesktopSidebar] = useState(true);
  const [searching, setSearching] = useState(false);
  const [search, setSearch] = useState('');
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState('');
  const [findPosition, setFindPosition] = useState(0);
  const [findNavigation, setFindNavigation] = useState(0);
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [rowMenuPosition, setRowMenuPosition] = useState({ left: 0, top: 0 });
  const [conversationAction, setConversationAction] = useState<PendingConversationAction | null>(null);
  const conversationActionSerial = useRef(0);
  const actionInFlight = useRef<{ serial: number; originSnapshot: ChatState } | null>(null);
  const [modal, setModal] = useState<ModalState>(null);
  const [renameValue, setRenameValue] = useState('');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draftEpochs = useRef<Record<string, string>>({});
  const draftSequences = useRef<Record<string, number>>({});
  const draftVersions = useRef<Record<string, number>>({});
  const [pending, setPending] = useState<PendingSend | null>(null);
  const [showLatest, setShowLatest] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);
  const editorMeasure = useRef<HTMLTextAreaElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const findInput = useRef<HTMLInputElement>(null);
  const renameComposition = useRef(false);
  const sidebar = useRef<HTMLElement>(null);
  const gamePicker = useRef<HTMLDetailsElement>(null);
  const main = useRef<HTMLElement>(null);
  const drawerOrigin = useRef<HTMLElement | null>(null);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 760px)').matches);
  const viewport = useRef<HTMLDivElement>(null);
  const readingColumn = useRef<HTMLDivElement>(null);
  const composerArea = useRef<HTMLDivElement>(null);
  const [composerHeight, setComposerHeight] = useState(133);
  const nearBottom = useRef(true);
  const previousConversation = useRef('');
  const readingPositions = useRef(new Map<string, ReadingPosition>());
  const pendingReadingRestore = useRef<string | null>(null);
  const scrollGeometry = useRef<ScrollGeometry | null>(null);
  const readerScrollIntent = useRef(false);
  const readingState = useRef({ conversationId: state.conversationId, loading: state.historyLoading,
    firstId: state.messages[0]?.id, focusRequest: state.focusRequest });
  readingState.current = { conversationId: state.conversationId, loading: state.historyLoading,
    firstId: state.messages[0]?.id, focusRequest: state.focusRequest };
  const composition = useRef(false);
  const lastSearch = useRef('');
  const readySent = useRef(false);
  const hydrated = useRef(false);
  const appliedFocus = useRef('');
  const focusHighlightNode = useRef<HTMLElement | null>(null);
  const focusHighlightTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const draft = drafts[state.conversationId] || '';
  const stagedImage = state.stagedImage?.conversationId === state.conversationId ? state.stagedImage : undefined;
  const [imageInputState, setImageInputState] = useState<ImageInputSnapshot>({ busy: false });
  const imageInputContext = useRef<ImageInputContext>({ conversationId: '', draftEpoch: '', draftDocument: '',
    supported: false, available: false });
  imageInputContext.current = { conversationId: state.conversationId,
    draftEpoch: state.draftEpochs[state.conversationId] || '', draftDocument: state.draftDocument,
    supported: state.canStageImageData, available: state.ready && !state.historyLoading && !state.busy
      && !state.imageImportBusy && !pending && !modal };
  const imageInputDispatch = useRef(dispatch);
  imageInputDispatch.current = dispatch;
  const imageInput = useRef<ImageInputController | null>(null);
  if (!imageInput.current) imageInput.current = new ImageInputController({
    context: () => imageInputContext.current,
    dispatch: (payload, callback) => imageInputDispatch.current('stageImageData', payload, callback),
    changed: setImageInputState,
  });
  const localImagePreparing = imageInputState.busy && imageInputState.origin?.conversationId === state.conversationId
    && imageInputState.origin.draftEpoch === imageInputContext.current.draftEpoch
    && imageInputState.origin.draftDocument === state.draftDocument;
  const imagePreparing = state.imagePreparing || localImagePreparing;
  const imageInputError = imageInputState.error?.conversationId === state.conversationId
    && imageInputState.error.draftEpoch === imageInputContext.current.draftEpoch
    && imageInputState.error.draftDocument === state.draftDocument ? imageInputState.error.message : '';
  useLayoutEffect(() => { imageInput.current?.syncContext(); },
    [state.conversationId, state.draftEpochs[state.conversationId], state.draftDocument]);
  useEffect(() => {
    const input = imageInput.current!;
    input.activate();
    // Preserve normal text drops; file drops outside the composer must never
    // navigate WebEngine away from the current conversation.
    const preventFileNavigation = (event: globalThis.DragEvent) => {
      if (hasFileTransfer(event.dataTransfer)) event.preventDefault();
    };
    window.addEventListener('dragover', preventFileNavigation);
    window.addEventListener('drop', preventFileNavigation);
    return () => {
      input.dispose();
      window.removeEventListener('dragover', preventFileNavigation);
      window.removeEventListener('drop', preventFileNavigation);
    };
  }, []);
  const pasteImage = (event: ReactClipboardEvent<HTMLTextAreaElement>) => {
    if (!event.clipboardData.files.length) return;
    event.preventDefault();
    imageInput.current?.add(event.clipboardData.files);
  };
  const dropImage = (event: ReactDragEvent<HTMLElement>) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    event.preventDefault();
    imageInput.current?.add(event.dataTransfer.files);
  };
  const rememberReadingPosition = useCallback(() => {
    const node = viewport.current, current = readingState.current;
    if (!node || current.loading || pendingReadingRestore.current
        || current.firstId && !node.querySelector(`[data-message-id="${CSS.escape(current.firstId)}"]`)) return;
    const positions = readingPositions.current;
    positions.delete(current.conversationId);
    positions.set(current.conversationId, captureReadingPosition(node, nearBottom.current));
    if (positions.size > 128) positions.delete(positions.keys().next().value!);
  }, []);
  const reconcileReadingPosition = useCallback(() => {
    const node = viewport.current, current = readingState.current;
    if (!node || current.loading || readerScrollIntent.current) return;
    const request = current.focusRequest;
    const key = request?.id ? `${request.serial}:${request.id}` : '';
    if (request?.id && appliedFocus.current !== key) {
      // Search can arrive before assistant-ui has committed its new message
      // nodes. Keep the explicit target pending ahead of any cached anchor.
      pendingReadingRestore.current = null;
      nearBottom.current = false;
      const target = node.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(request.id)}"]`);
      if (target) {
        clearTimeout(focusHighlightTimer.current);
        focusHighlightNode.current?.classList.remove('message-highlight');
        focusHighlightNode.current = target;
        appliedFocus.current = key;
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.classList.add('message-highlight');
        focusHighlightTimer.current = setTimeout(() => {
          target.classList.remove('message-highlight');
          if (focusHighlightNode.current === target) focusHighlightNode.current = null;
        }, 2200);
      }
      return;
    }
    if (current.firstId && !node.querySelector(`[data-message-id="${CSS.escape(current.firstId)}"]`)) return;
    const saved = readingPositions.current.get(current.conversationId);
    if (pendingReadingRestore.current === current.conversationId) {
      nearBottom.current = saved?.following ?? true;
      pendingReadingRestore.current = null;
      if (saved && !nearBottom.current) restoreReadingPosition(node, saved);
    }
    if (nearBottom.current) node.scrollTop = node.scrollHeight;
    if (node.scrollHeight <= node.clientHeight + 1) nearBottom.current = true;
    scrollGeometry.current = { conversationId: current.conversationId, top: node.scrollTop,
      height: node.scrollHeight, client: node.clientHeight };
    setShowLatest(!nearBottom.current);
    rememberReadingPosition();
  }, [rememberReadingPosition]);
  const cancelPendingMessageFocus = useCallback(() => {
    const request = readingState.current.focusRequest;
    if (request?.id) appliedFocus.current = `${request.serial}:${request.id}`;
  }, []);
  const noteReaderScroll = useCallback(() => {
    readerScrollIntent.current = true;
    cancelPendingMessageFocus();
    // User input wins over an in-flight restoration. Scroll events run before
    // animation frames; an inner code block's wheel must not leave a stale flag.
    pendingReadingRestore.current = null;
    requestAnimationFrame(() => { readerScrollIntent.current = false; reconcileReadingPosition(); });
  }, [cancelPendingMessageFocus, reconcileReadingPosition]);
  const canSteer = state.canSteerAgent === true && state.requestInCurrent && !state.agentInteraction && !stagedImage;
  const canSend = state.ready && (!state.busy || canSteer) && !state.historyLoading && !imagePreparing && !pending && Boolean(draft.trim() || stagedImage);
  const findIds = useMemo(() => {
    const query = findQuery.trim().toLocaleLowerCase();
    return findOpen && query ? state.messages.filter((message) =>
      message.content.toLocaleLowerCase().includes(query)).map((message) => message.id) : EMPTY_FIND_RESULTS;
  }, [findOpen, findQuery, state.messages]);
  const findMatches = useMemo(() => new Set(findIds), [findIds]);
  const findIndex = Math.min(findPosition, Math.max(0, findIds.length - 1));
  const findMatchId = findIds[findIndex];
  const displayedError = useMemo<DisplayedErrorBinding | undefined>(() =>
    state.ready && state.error && state.errorConversationId === state.conversationId && state.errorMessageId
      ? { conversationId: state.errorConversationId, messageId: state.errorMessageId, error: state.error,
        retryStatus: state.errorAction === 'retry' ? state.errorRetryStatus || '' : '' }
      : undefined,
  [state.ready, state.error, state.errorConversationId, state.conversationId, state.errorMessageId,
    state.errorAction, state.errorRetryStatus]);
  const hostContext = useMemo(() => ({ dispatch, conversationId: state.conversationId,
    busy: state.busy, canBudget: state.canBudget, focusRequest: state.focusRequest, findMatches, findMatchId, displayedError }),
  [dispatch, state.conversationId, state.busy, state.canBudget, state.focusRequest, findMatches, findMatchId, displayedError]);
  const moveFind = useCallback((direction: number) => {
    if (!findIds.length) return;
    setFindPosition((value) => (Math.min(value, findIds.length - 1) + direction + findIds.length) % findIds.length);
    setFindNavigation((value) => value + 1);
  }, [findIds.length]);
  const openFind = useCallback(() => {
    cancelPendingMessageFocus();
    setFindOpen(true); setRowMenu(null); setSidebarOpen(false);
    requestAnimationFrame(() => { findInput.current?.focus(); findInput.current?.select(); });
  }, [cancelPendingMessageFocus]);
  const closeFind = useCallback(() => { setFindOpen(false); editor.current?.focus(); }, []);
  useEffect(() => {
    setFindOpen(false); setFindQuery(''); setFindPosition(0);
  }, [state.conversationId]);
  const cancelConversationAction = useCallback(() => {
    conversationActionSerial.current += 1;
    actionInFlight.current = null;
    setConversationAction(null);
  }, []);

  const runConversationAction = useCallback((id: string, kind: ConversationAction) => {
    if (id !== state.conversationId || state.historyLoading || !state.ready) return;
    if (kind === 'find') openFind();
    else if (!state.busy && state.messages.length && !(kind === 'export' && state.exportBusy)) {
      dispatch(kind, { conversationId: id });
    }
  }, [state.conversationId, state.historyLoading, state.ready, state.busy,
    state.messages.length, state.exportBusy, dispatch, openFind]);

  const selectConversationAction = (id: string, kind: ConversationAction) => {
    cancelConversationAction();
    setRowMenu(null); setSidebarOpen(false);
    if (!state.ready || state.historyLoading) return;
    if (id === state.conversationId) {
      runConversationAction(id, kind);
      return;
    }
    const intent: PendingConversationAction = { conversationId: id, kind,
      serial: conversationActionSerial.current, accepted: false };
    actionInFlight.current = { serial: intent.serial, originSnapshot: state };
    setConversationAction(intent);
    dispatch('open', { id }, (result) => {
      if (actionInFlight.current?.serial !== intent.serial) return;
      if (!result.ok) cancelConversationAction();
      else setConversationAction({ ...intent, accepted: true });
    });
  };

  useEffect(() => {
    if (!conversationAction || !conversationAction.accepted
      || actionInFlight.current?.serial !== conversationAction.serial) return;
    // The open acknowledgement can arrive before its new state snapshot. Never
    // act on the pre-click view, or keep an intent after navigation restores it.
    if (state === actionInFlight.current.originSnapshot) return;
    const target = conversationAction.conversationId;
    if (state.errorAction === 'history' && state.errorConversationId === target) {
      cancelConversationAction();
      return;
    }
    if (state.conversationId !== target) {
      cancelConversationAction();
      return;
    }
    if (state.historyLoading) return;
    // Consume first: state refreshes and bridge callbacks cannot replay an action.
    actionInFlight.current = null;
    setConversationAction(null);
    runConversationAction(target, conversationAction.kind);
  }, [conversationAction, state, runConversationAction, cancelConversationAction]);

  useEffect(() => {
    if (!findOpen || !findMatchId || state.historyLoading) return;
    const frame = requestAnimationFrame(() => {
      const node = viewport.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(findMatchId)}"]`);
      if (node) {
        pendingReadingRestore.current = null;
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
    if (state.ready && !readySent.current) {
      readySent.current = true;
      // Refresh history before announcing readiness, rather than relying on a
      // delayed empty search caused by the bridge callback changing identity.
      dispatch('search', { query: '' });
      dispatch('ready', { draftDocument: state.draftDocument });
    }
  }, [state.ready, state.drafts, state.draftDocument, dispatch]);

  useEffect(() => {
    if (!state.ready) return;
    setDrafts((previous) => {
      let next = previous;
      for (const [cid, epoch] of Object.entries(state.draftEpochs)) {
        const changedEpoch = draftEpochs.current[cid] !== epoch;
        const version = state.draftVersions[cid] || 0;
        const acknowledged = state.draftSequences[cid] || 0;
        const localSequence = draftSequences.current[cid] || 0;
        // An older state snapshot must not erase a later input event still
        // travelling through WebChannel. An explicit clear rotates the epoch.
        if (changedEpoch || version > (draftVersions.current[cid] || 0) && acknowledged >= localSequence) {
          const text = state.drafts[cid] || '';
          if ((next[cid] || '') !== text) next = { ...next, [cid]: text };
          draftVersions.current[cid] = version;
        }
        if (changedEpoch) {
          draftEpochs.current[cid] = epoch;
          draftSequences.current[cid] = acknowledged;
        }
      }
      return next;
    });
  }, [state.ready, state.drafts, state.draftEpochs, state.draftVersions, state.draftSequences]);

  const submitMessage = useCallback(async (text: string) => {
    if ((!text && !stagedImage) || state.busy && !canSteer || state.historyLoading || imagePreparing
        || imageInput.current?.preparingCurrent() || pending) return;
    const sequence = draftSequences.current[state.conversationId] || state.draftSequences[state.conversationId] || 0;
    const record = { conversationId: state.conversationId, text, draftSequence: sequence };
    setPending(record);
    cancelPendingMessageFocus();
    pendingReadingRestore.current = null;
    nearBottom.current = true;
    await new Promise<void>((resolve) => {
      dispatch(state.busy ? 'steerAgent' : 'send', { text, conversationId: state.conversationId,
        draftEpoch: state.draftEpochs[state.conversationId], draftSequence: sequence,
        draftBaseVersion: draftVersions.current[state.conversationId] || state.draftVersions[state.conversationId] || 1,
        draftDocument: state.draftDocument,
        ...(!state.busy ? { agentMode: state.agentMode } : {}) }, (result) => {
        if (result.ok) setDrafts((value) => value[record.conversationId]?.trim() === text
          && (draftSequences.current[record.conversationId] || 0) === record.draftSequence
          ? { ...value, [record.conversationId]: '' } : value);
        setPending(null);
        resolve();
      });
    });
  }, [state.busy, state.historyLoading, imagePreparing, state.conversationId, state.agentMode,
    state.draftEpochs, state.draftSequences, state.draftVersions, state.draftDocument, stagedImage, pending, dispatch, canSteer, cancelPendingMessageFocus]);
  const onNew = useCallback((message: AppendMessage) => submitMessage(message.content
    .filter((part) => part.type === 'text').map((part) => part.text).join('\n').trim()), [submitMessage]);

  const convertedMessageCache = useRef(new WeakMap<ChatMessage, {
    execution: ExecutionTrace | undefined; message: ThreadMessageLike;
  }>());
  const historyMessages = useMemo(() => {
    const messages: ChatMessage[] = state.messages.filter((item) => item.role === 'user' || item.role === 'assistant');
    const traces = new Map(messages.filter((item) => item.metadata?.execution).map((item) => [item.id, item.metadata!.execution!]));
    const users = new Set(messages.filter((item) => item.role === 'user' &&
      (!item.source || item.source === 'user')).map((item) => item.id));
    const replies = messages.filter((item) => item.role === 'assistant' && (!item.source || item.source === 'assistant'));
    const requests = new Map(Array.from(users, (id) => [id, id]));
    const linkedUsers = new Set<string>();
    replies.forEach((item) => {
      const target = item.metadata?.reply_to;
      if (typeof target === 'string' && users.has(target)) {
        requests.set(item.id, target);
        linkedUsers.add(target);
      }
    });
    // Explicit request links survive queued reply order. Only an absent link
    // may inherit the first ordinary reply within a legacy adjacent turn.
    let legacyRequest: string | undefined;
    messages.forEach((item) => {
      if (item.role === 'user') {
        legacyRequest = users.has(item.id) && !linkedUsers.has(item.id) ? item.id : undefined;
      } else if ((!item.source || item.source === 'assistant') && legacyRequest !== undefined && item.metadata?.reply_to === undefined) {
        requests.set(item.id, legacyRequest);
        legacyRequest = undefined;
      }
    });
    replies.forEach((item) => {
      const trace = traces.get(requests.get(item.id) || '');
      if (trace && ['completed', 'needs_user'].includes(trace.state) && !traces.has(item.id)) traces.set(item.id, trace);
    });
    const owners = new Map<string, string>();
    const executionKeys = new Map<string, string>();
    messages.forEach((item) => {
      const trace = traces.get(item.id);
      if (!trace) return;
      const key = JSON.stringify([requests.get(item.id) ?? item.id, trace.started_at || item.id]);
      executionKeys.set(item.id, key);
      if (!owners.has(key) || item.role === 'assistant') owners.set(key, item.id);
    });
    return messages.map((item): ThreadMessageLike => {
      const execution = owners.get(executionKeys.get(item.id) || '') === item.id ? traces.get(item.id) : undefined;
      const cached = convertedMessageCache.current.get(item);
      if (cached && cached.execution === execution) return cached.message;
      const converted: ThreadMessageLike = {
        id: String(item.id), role: item.role,
        content: [{ type: 'text', text: String(item.content ?? '') }],
        metadata: { custom: { source: item.source, screen_event: item.screen_event, request_status: item.metadata?.request_status,
          agent_error_code: item.metadata?.agent_error_code, agent_error: item.metadata?.agent_error,
          streaming_status: item.metadata?.streaming_status,
          selection_context: item.metadata?.selection_context,
          image_id: item.metadata?.image_id, image_revision: item.metadata?.image_revision, image_error: item.metadata?.image_error,
          image_unavailable: item.metadata?.image_unavailable,
          image_thumbnail: item.metadata?.image_thumbnail, image_thumbnail_error: item.metadata?.image_thumbnail_error,
          execution,
          retryAvailable: item.retryAvailable, retryStatus: item.retryStatus } },
      };
      convertedMessageCache.current.set(item, { execution, message: converted });
      return converted;
    });
  }, [state.messages]);

  const runtimeMessages = useMemo<ThreadMessageLike[]>(() => {
    const preview = state.streamingReply;
    if (!preview || preview.conversationId !== state.conversationId || !preview.text) return historyMessages;
    return [...historyMessages, {
      id: `stream-${preview.requestId}`, role: 'assistant',
      content: [{ type: 'text', text: preview.text }],
      metadata: { custom: { source: 'stream_preview', streaming_status: preview.state } },
    }];
  }, [historyMessages, state.streamingReply, state.conversationId]);

  const runtime = useExternalStoreRuntime({
    messages: runtimeMessages,
    convertMessage: identityMessage,
    isRunning: state.busy && state.requestInCurrent,
    onNew,
  });
  const hasRunningProcess = useMemo(() => state.requestInCurrent && state.messages.some((message) => message.metadata?.execution?.state === 'running'),
    [state.requestInCurrent, state.messages]);

  useEffect(() => {
    if (!pending) return;
    if (state.error && !state.busy) {
      setPending(null);
    }
  }, [state, pending]);

  useEffect(() => {
    if (search === lastSearch.current) return;
    const timeout = setTimeout(() => {
      if (dispatch('search', { query: search })) lastSearch.current = search;
    }, 250);
    return () => clearTimeout(timeout);
  }, [search, dispatch]);

  const resizeEditor = useCallback(() => {
    const input = editor.current;
    const measure = editorMeasure.current;
    if (!input || !measure || !input.clientWidth) return;
    // Measure a hidden mirror instead of collapsing the live editor on every
    // keystroke. Its value/styles match the editor, including trailing newlines.
    const width = `${input.getBoundingClientRect().width}px`;
    if (measure.style.width !== width) measure.style.width = width;
    const height = `${Math.min(measure.scrollHeight, 160)}px`;
    if (input.style.height !== height) input.style.height = height;
  }, []);

  useLayoutEffect(resizeEditor, [draft, state.chatFontSize, resizeEditor]);

  useEffect(() => {
    // A restored draft may precede the final font metrics. Keep measuring the
    // hidden mirror; never collapse the live input or move its selection.
    let mounted = true;
    const fonts = document.fonts;
    if (!fonts) return;
    void fonts.ready.then(() => { if (mounted) resizeEditor(); });
    fonts.addEventListener('loadingdone', resizeEditor);
    return () => { mounted = false; fonts.removeEventListener('loadingdone', resizeEditor); };
  }, [resizeEditor]);

  useEffect(() => {
    let previousWidth = 0;
    const observer = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width;
      if (width !== previousWidth) { previousWidth = width; resizeEditor(); }
    });
    if (editor.current) observer.observe(editor.current);
    return () => observer.disconnect();
  }, [resizeEditor]);

  useLayoutEffect(() => {
    if (state.historyLoading) {
      pendingReadingRestore.current = state.conversationId;
      return;
    }
    if (previousConversation.current !== state.conversationId) pendingReadingRestore.current = state.conversationId;
    previousConversation.current = state.conversationId;
    reconcileReadingPosition();
    const frame = requestAnimationFrame(reconcileReadingPosition);
    return () => cancelAnimationFrame(frame);
  }, [state.messages, state.conversationId, state.historyLoading, reconcileReadingPosition]);

  useEffect(() => {
    const observer = new ResizeObserver(reconcileReadingPosition);
    const contentReady = new MutationObserver(() => {
      const request = readingState.current.focusRequest;
      if (pendingReadingRestore.current || request?.id && appliedFocus.current !== `${request.serial}:${request.id}`) {
        reconcileReadingPosition();
      }
    });
    if (readingColumn.current) observer.observe(readingColumn.current);
    if (viewport.current) observer.observe(viewport.current);
    if (readingColumn.current) contentReady.observe(readingColumn.current, { childList: true, subtree: true });
    return () => { observer.disconnect(); contentReady.disconnect(); };
  }, [reconcileReadingPosition]);

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
    reconcileReadingPosition();
    const frame = requestAnimationFrame(reconcileReadingPosition);
    return () => cancelAnimationFrame(frame);
  }, [state.focusRequest?.id, state.focusRequest?.serial, state.messages, reconcileReadingPosition]);

  const openSearch = useCallback(() => {
    cancelConversationAction();
    setSearching(true);
    setSidebarOpen(true);
    setDesktopSidebar(true);
    setTimeout(() => { searchInput.current?.focus(); searchInput.current?.select(); }, 30);
  }, [cancelConversationAction]);

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
        if (gamePicker.current?.open) {
          gamePicker.current.open = false;
          gamePicker.current.querySelector('summary')?.focus();
          return;
        }
        cancelConversationAction(); setSidebarOpen(false); setRowMenu(null); return;
      }
      if (event.key !== 'Tab') return;
      const items = [...panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href], summary'),
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
      // Restore after React has removed the modal drawer and its inert state.
      // WebEngine can otherwise retain the editor focus set by Escape's handler.
      requestAnimationFrame(() => {
        if (origin?.isConnected && !background?.inert) origin.focus({ preventScroll: true });
      });
    };
  }, [sidebarOpen, narrow, cancelConversationAction]);

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if (modal || document.querySelector('dialog[open]')) {
        if ((event.ctrlKey || event.metaKey) && ['n', 'f'].includes(event.key.toLowerCase())) event.preventDefault();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        cancelConversationAction();
        if (!state.historyLoading && state.ready) dispatch('new');
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f' && event.shiftKey) {
        event.preventDefault(); cancelConversationAction(); if (state.ready && !state.historyLoading) openFind();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault(); openSearch();
      } else if (event.key === 'F3' && findOpen) {
        event.preventDefault(); moveFind(event.shiftKey ? -1 : 1);
      } else if (event.key === 'Escape' && findOpen) {
        event.preventDefault(); cancelConversationAction(); closeFind();
      } else if (event.key === 'Escape' && !modal) {
        cancelConversationAction(); setRowMenu(null); setSidebarOpen(false);
        if (searching) { setSearching(false); setSearch(''); }
        editor.current?.focus();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [state.historyLoading, state.ready, dispatch, openSearch, modal, searching, openFind, closeFind,
    moveFind, findOpen, cancelConversationAction]);

  useEffect(() => {
    if (!rowMenu) return;
    const close = (event: PointerEvent) => {
      if (!(event.target as Element).closest('[data-popover]')) setRowMenu(null);
    };
    window.addEventListener('pointerdown', close);
    return () => window.removeEventListener('pointerdown', close);
  }, [rowMenu]);

  useEffect(() => {
    if (!rowMenu) return;
    const frame = requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('.row-popover button:not(:disabled)')?.focus());
    const navigate = (event: globalThis.KeyboardEvent) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const menu = document.querySelector('.row-popover');
      if (!menu?.contains(document.activeElement)) return;
      const buttons = [...menu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      event.preventDefault(); buttons[next]?.focus();
    };
    window.addEventListener('keydown', navigate);
    return () => { cancelAnimationFrame(frame); window.removeEventListener('keydown', navigate); };
  }, [rowMenu]);

  useLayoutEffect(() => {
    if (!rowMenu) return;
    const place = () => {
      const menu = document.querySelector<HTMLElement>('.row-popover');
      if (!menu) return;
      const bounds = menu.getBoundingClientRect();
      const left = Math.max(8, Math.min(window.innerWidth - bounds.width - 8, bounds.left));
      const top = Math.max(8, Math.min(window.innerHeight - bounds.height - 8, bounds.top));
      setRowMenuPosition((previous) => previous.left === left && previous.top === top ? previous : { left, top });
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [rowMenu]);

  const send = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend || composition.current || imageInput.current?.preparingCurrent()) return;
    if (state.busy && canSteer) void submitMessage(draft.trim());
    else if (!draft.trim() && stagedImage) void submitMessage('');
    else runtime.thread.append({ role: 'user', content: [{ type: 'text', text: draft.trim() }] });
    editor.current?.focus();
  };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229 && !composition.current) {
      event.preventDefault();
      if (!event.repeat) send();
    }
  };
  const showModal = (value: ModalState) => {
    cancelConversationAction(); setModal(value); setRowMenu(null); setSidebarOpen(false);
    renameComposition.current = false;
    if (value?.kind === 'rename') setRenameValue(value.title);
  };
  const closeModal = () => {
    setModal(null);
    // A modal dialog rejects background focus until React removes it.
    requestAnimationFrame(() => editor.current?.focus());
  };
  const goLatest = () => {
    cancelPendingMessageFocus();
    pendingReadingRestore.current = null; nearBottom.current = true;
    viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: 'smooth' }); setShowLatest(false);
  };
  const navigateMessage = useCallback((id: string) => {
    const node = viewport.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(id)}"]`);
    if (!node) return;
    nearBottom.current = false;
    clearTimeout(focusHighlightTimer.current);
    focusHighlightNode.current?.classList.remove('message-highlight');
    focusHighlightNode.current = null;
    node.scrollIntoView({ block: 'start', behavior: 'instant' });
    const scroller = viewport.current!;
    nearBottom.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 72;
    setShowLatest(!nearBottom.current);
  }, []);

  const recoveryLabel = ({ settings: '打开 API 设置', history: '重新读取', retry: '重试回复' } as Record<string, string>)[state.errorAction];
  const headerAction = (event: ReactPointerEvent<HTMLElement> | ReactMouseEvent<HTMLElement>, action: string) => {
    if (!state.windowChrome || event.button !== 0 || !(event.target instanceof Element)) return;
    if (event.target.closest('button, input, textarea, a, [role="menu"], [data-popover]')) return;
    if (action === 'windowMove' && event.detail > 1) return;
    event.preventDefault();
    dispatch(action);
  };
  const recover = () => {
    if (state.errorAction === 'settings') dispatch('settings');
    else if (state.errorAction === 'history') dispatch('reloadHistory', { conversationId: state.errorConversationId });
    else if (state.errorAction === 'retry') dispatch('retry', { conversationId: state.errorConversationId, messageId: state.errorMessageId });
  };

  return <AssistantRuntimeProvider runtime={runtime}><HostContext.Provider value={hostContext}>
    <div className={`chat-shell ${state.windowChrome ? 'has-window-chrome' : ''} ${desktopSidebar ? '' : 'sidebar-collapsed'} ${sidebarOpen ? 'sidebar-open' : ''}`}
      style={{ '--chat-font-size': `${state.chatFontSize || 16}px` } as CSSProperties}>
      <a className="skip-link" href="#conversation">跳到对话</a>
      {sidebarOpen && <button className="sidebar-backdrop" tabIndex={-1} aria-label="关闭历史会话" onClick={() => setSidebarOpen(false)} />}
      <aside ref={sidebar} className="sidebar" data-testid="sidebar" aria-label="历史会话" role={sidebarOpen && narrow ? 'dialog' : undefined} aria-modal={sidebarOpen && narrow || undefined}>
        <div className="sidebar-top" onPointerDown={(event) => headerAction(event, 'windowMove')} onDoubleClick={(event) => headerAction(event, 'windowToggleMaximize')}>
          <span className="wordmark">鲸伴</span>
          <div className="sidebar-controls"><IconButton label="搜索会话 · Ctrl+F" onClick={openSearch}><Search size={17} /></IconButton><IconButton className="close-sidebar" label="收起历史会话" onClick={() => { setSidebarOpen(false); setDesktopSidebar(false); }}><PanelLeftClose size={18} /></IconButton></div>
        </div>
        <button type="button" className="new-conversation" title="新对话 · Ctrl+N" disabled={!state.ready || state.historyLoading} onClick={() => { cancelConversationAction(); dispatch('new'); setSidebarOpen(false); }}><Plus size={17} /><span>新对话</span></button>
        {searching && <div className="search-field"><Search size={15} /><input data-testid="conversation-search" ref={searchInput} value={search} onChange={(event) => { cancelConversationAction(); setSearch(event.target.value); }} placeholder="搜索对话" aria-label="搜索历史会话" /><IconButton label="关闭搜索" onClick={() => { setSearching(false); setSearch(''); }}><X size={14} /></IconButton></div>}
        <nav className="conversation-list" aria-label="会话列表">
          {state.conversations.map((item) => <div key={item.id} data-conversation-id={item.id} className={`conversation-row ${item.id === state.conversationId ? 'selected' : ''} ${rowMenu === item.id ? 'menu-active' : ''}`}>
            <button type="button" className="conversation-link" title={item.title || '新对话'} aria-current={item.id === state.conversationId ? 'page' : undefined} disabled={state.historyLoading} onClick={() => { rememberReadingPosition(); cancelPendingMessageFocus(); cancelConversationAction(); dispatch('open', { id: item.id }); setSidebarOpen(false); setRowMenu(null); }}>
              <span className="conversation-title">{item.title || '新对话'}</span>
            </button>
            <div className="row-actions" data-popover><IconButton label={`管理会话：${item.title || '新对话'}`} onClick={(event) => {
              const bounds = event.currentTarget.getBoundingClientRect();
              cancelConversationAction();
              setRowMenuPosition({ left: Math.max(8, bounds.right - 218), top: Math.max(8, bounds.bottom + 4) });
              setRowMenu(rowMenu === item.id ? null : item.id);
            }} aria-expanded={rowMenu === item.id}><MoreHorizontal size={16} /></IconButton>
              {rowMenu === item.id && createPortal(<GlassSurface className="popover row-popover" radius={17} intensity={16} data-popover role="menu" aria-label={`会话操作：${item.title || '新对话'}`} style={rowMenuPosition}>
                <button role="menuitem" data-testid="row-action-find" disabled={!state.ready || state.historyLoading} onClick={() => selectConversationAction(item.id, 'find')}><Search size={15} />查找对话</button>
                {state.canExport && <button role="menuitem" data-testid="row-action-export" disabled={!state.ready || state.busy || state.historyLoading || state.exportBusy || (item.id === state.conversationId && !state.messages.length)} onClick={() => selectConversationAction(item.id, 'export')}><Download size={15} />导出对话</button>}
                <button role="menuitem" data-testid="row-action-rename" onClick={() => showModal({ kind: 'rename', id: item.id, title: item.title })} disabled={state.historyLoading}><Pencil size={15} />重命名</button>
                <div className="menu-divider" />
                <button role="menuitem" data-testid="row-action-clear" className="danger" disabled={!state.ready || state.busy || state.historyLoading || (item.id === state.conversationId && !state.messages.length)} onClick={() => selectConversationAction(item.id, 'clear')}><Trash2 size={15} />清空对话</button>
                <button role="menuitem" data-testid="row-action-delete" className="danger" onClick={() => { cancelConversationAction(); dispatch('delete', { id: item.id }); setRowMenu(null); }} disabled={state.busy || state.historyLoading}><Trash2 size={15} />删除会话</button>
              </GlassSurface>, document.body)}
            </div>
          </div>)}
          {state.ready && !state.conversations.length && <p className="list-empty">{search ? '没有找到相关对话' : '新对话会保存在这里'}</p>}
        </nav>
        <div className="sidebar-bottom">
          {(state.canXiangqi || state.canGomoku) && <details className="sidebar-games" ref={gamePicker} onKeyDown={(event) => {
            if (event.key === 'Escape' && gamePicker.current?.open) {
              event.preventDefault(); event.stopPropagation();
              gamePicker.current.open = false;
              gamePicker.current.querySelector('summary')?.focus();
            }
          }}>
            <summary className="sidebar-setting" data-testid="open-games"><Gamepad2 size={16} /><span>和鲸鱼娘玩耍</span><ChevronDown size={14} className="sidebar-game-chevron" aria-hidden="true" /></summary>
            <div className="sidebar-game-choices">
              {state.canXiangqi && <button className="sidebar-game-choice" data-testid="open-xiangqi" aria-label="和鲸鱼娘下象棋" onClick={() => {
                if (gamePicker.current) {
                  gamePicker.current.open = false;
                  gamePicker.current.querySelector('summary')?.focus({ preventScroll: true });
                }
                cancelConversationAction(); dispatch('openXiangqi');
              }}>象棋</button>}
              {state.canGomoku && <button className="sidebar-game-choice" data-testid="open-gomoku" aria-label="和鲸鱼娘下五子棋" onClick={() => {
                if (gamePicker.current) {
                  gamePicker.current.open = false;
                  gamePicker.current.querySelector('summary')?.focus({ preventScroll: true });
                }
                cancelConversationAction(); dispatch('openGomoku');
              }}>五子棋</button>}
            </div>
          </details>}
          {state.canSettings && <button className="sidebar-setting" onClick={() => { cancelConversationAction(); dispatch('openSettings'); }}><Settings2 size={16} /><span>设置</span></button>}
        </div>
      </aside>

      <main ref={main} className="main-panel" id="conversation" onDrop={dropImage}>
        <header className="chat-header" onPointerDown={(event) => headerAction(event, 'windowMove')} onDoubleClick={(event) => headerAction(event, 'windowToggleMaximize')}>
          <IconButton data-testid="history-toggle" className="show-sidebar" label="展开历史会话" onClick={() => { setSidebarOpen(true); setDesktopSidebar(true); }}><Menu size={20} /></IconButton>
          <div className="header-title" title={state.title || '鲸伴'}>{state.title || '鲸伴'}</div>
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
          <IconButton label="关闭对话内查找" onClick={() => { cancelConversationAction(); closeFind(); }}><X size={16} /></IconButton>
        </div>}
        <ThreadPrimitive.Root className="thread">
          <ConversationRail key={state.conversationId} messages={state.messages} conversationId={state.conversationId}
            viewportRef={viewport} contentRef={readingColumn} bottomInset={composerHeight + 8} onNavigate={navigateMessage}
            disabled={!state.ready || state.historyLoading || findOpen || Boolean(modal) || Boolean(rowMenu) || (narrow && sidebarOpen)} />
          <div ref={viewport} className="thread-viewport" onWheel={noteReaderScroll} onTouchStart={noteReaderScroll}
            onPointerDown={noteReaderScroll} onKeyDownCapture={(event) => {
              if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) noteReaderScroll();
            }} onScroll={() => {
            const node = viewport.current!;
            const current = readingState.current, previous = scrollGeometry.current;
            if (current.loading || pendingReadingRestore.current) return;
            const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 72;
            const sameGeometry = previous?.conversationId === current.conversationId
              && previous.height === node.scrollHeight && previous.client === node.clientHeight;
            // Appending a bubble can dispatch a delayed scroll event while the
            // content is growing. Geometry alone must not cancel Send's intent.
            if (atBottom) nearBottom.current = true;
            else if (readerScrollIntent.current || sameGeometry && node.scrollTop < previous!.top - 1) nearBottom.current = false;
            scrollGeometry.current = { conversationId: current.conversationId, top: node.scrollTop,
              height: node.scrollHeight, client: node.clientHeight };
            setShowLatest(!nearBottom.current);
            rememberReadingPosition();
          }}>
            <div className="reading-column" ref={readingColumn}>
              {!state.ready ? <div className="empty-state">{connectionError ? <><h1>暂时无法连接</h1><p>{connectionError}</p></> : <><h1>正在连接</h1><p className="loading-text"><LoaderCircle size={16} className="spin" />正在读取对话</p></>}</div> : state.historyLoading ? <div className="history-loading" role="status"><LoaderCircle size={19} className="spin" />正在读取对话…</div> : !state.messages.length ? <div className="empty-state"><h1>今天想聊些什么？</h1></div> : <ThreadPrimitive.Messages components={{ UserMessage: ChatMessageView, AssistantMessage: ChatMessageView }} />}
              {!!state.agentArtifacts?.length && <AgentArtifactCards key={state.conversationId} artifacts={state.agentArtifacts}
                conversationId={state.conversationId} enabled={state.canExportArtifact === true} dispatch={dispatch} />}
              {state.agentInteraction && <AgentInteractionCard key={`${state.conversationId}:${state.agentInteraction.id}`}
                interaction={state.agentInteraction} dispatch={dispatch} />}
            </div>
          </div>
          {showLatest && <button className="latest-button" style={{ bottom: composerHeight + 12 }} type="button" onClick={goLatest}><ArrowDown size={16} />回到最新</button>}
          <div className="composer-area" ref={composerArea}>
            <div className="composer-column">
              {(state.error || connectionError) && state.ready && <div className="error-banner" role="alert">
                <span>{state.error || connectionError}
                  {state.errorAction === 'retry' && state.errorRetryStatus
                    && !(state.error || connectionError).includes(state.errorRetryStatus)
                    && <small className="retry-explanation">{state.errorRetryStatus}</small>}
                </span>
                {state.error && recoveryLabel && <button type="button"
                  disabled={state.busy || state.historyLoading || (state.errorAction === 'retry' && state.errorRetryAvailable === false)}
                  onClick={recover}>{recoveryLabel}</button>}
              </div>}
              {state.exportStatus && <p className="export-status" role="status">{state.exportStatus}</p>}
              {state.canAgentMode && <div className="chat-mode-row">
                <div className="chat-mode-selector" role="group" aria-label="对话模式">
                  <button type="button" data-testid="chat-mode-chat" aria-pressed={!state.agentMode}
                    disabled={!state.ready || state.busy || state.historyLoading || Boolean(pending)}
                    onClick={() => dispatch('setAgentMode', { conversationId: state.conversationId, enabled: false })}>聊天</button>
                  <button type="button" data-testid="chat-mode-agent" aria-pressed={state.agentMode}
                    disabled={!state.ready || state.busy || state.historyLoading || Boolean(pending)}
                    onClick={() => dispatch('setAgentMode', { conversationId: state.conversationId, enabled: true })}>电脑 Agent</button>
                </div>
              </div>}
              <GlassSurface className="glass-composer" radius={27} intensity={18}>
              <form className={`composer ${state.busy ? 'composer-busy' : ''}`} onSubmit={send}>
                {(stagedImage || imagePreparing) && <div className="composer-attachment" data-testid="composer-attachment">
                  {stagedImage?.thumbnail ? <img src={stagedImage.thumbnail.src} alt="待发送图片" draggable={false} />
                    : <span className="composer-attachment-placeholder" aria-hidden="true"><Plus size={20} /></span>}
                  <div className="composer-attachment-details"><strong title={stagedImage?.filename}>{stagedImage?.filename || '正在准备图片…'}</strong>
                    <span>{imagePreparing ? '正在准备，完成后可发送' : '待发送 · 可以添加问题，也可直接发送'}</span></div>
                  {stagedImage && <IconButton label="移除待发送图片" data-testid="remove-composer-image"
                    disabled={imagePreparing || Boolean(pending) || state.busy}
                    onClick={() => dispatch('removeStagedImage', { conversationId: state.conversationId })}><X size={17} /></IconButton>}
                </div>}
                {(imageInputError || state.imageError) && <p className="composer-image-error" role="alert" data-testid="composer-image-error">{imageInputError || state.imageError}</p>}
                <label htmlFor="message-input" className="sr-only">发送给鲸伴的消息</label>
                <textarea id="message-input" data-testid="message-input" ref={editor} rows={1} value={draft} placeholder={state.historyLoading ? '正在读取对话…' : canSteer ? '补充当前任务…' : state.agentMode ? '告诉电脑 Agent 下一步…' : '发消息…'} disabled={!state.ready || state.historyLoading} onChange={(event) => {
                  const text = event.target.value;
                  setDrafts((value) => ({ ...value, [state.conversationId]: text }));
                  const sequence = Math.max(draftSequences.current[state.conversationId] || 0,
                    state.draftSequences[state.conversationId] || 0) + 1;
                  draftSequences.current[state.conversationId] = sequence;
                  dispatch('draft', { conversationId: state.conversationId, text,
                    draftEpoch: state.draftEpochs[state.conversationId], draftSequence: sequence,
                    draftBaseVersion: draftVersions.current[state.conversationId] || state.draftVersions[state.conversationId] || 1,
                    draftDocument: state.draftDocument });
                }} onPaste={pasteImage} onKeyDown={keyDown} onCompositionStart={() => { composition.current = true; }} onCompositionEnd={() => { composition.current = false; }} />
                <textarea ref={editorMeasure} className="composer-measure" aria-hidden="true" tabIndex={-1}
                  rows={1} value={draft} readOnly />
                <div className="composer-toolbar"><IconButton label="添加图片" data-testid="composer-image-picker"
                  disabled={!state.ready || state.historyLoading || state.imageImportBusy || imageInputState.busy || state.busy || Boolean(pending)}
                  onClick={() => { if (!imageInput.current?.busy()) dispatch('selectImage', { conversationId: state.conversationId }); }}><Plus size={18} /></IconButton>
                  {(state.busy || pending) && !hasRunningProcess && <div className="composer-state" role="status"><LoaderCircle size={13} className="spin" aria-hidden="true" /><span>{state.status || (state.agentMode ? '电脑 Agent 正在执行…' : '正在回复…')}</span></div>}<div className="composer-actions">{state.canStop && <button type="button" data-testid="stop-button" className="stop-button" onClick={() => dispatch('stop', { conversationId: state.requestConversationId })} title={state.agentMode ? '停止当前电脑任务' : '停止等待本次回复；服务端可能已收到请求'}><Square size={12} aria-hidden="true" />{state.agentMode ? '停止任务' : '停止生成'}</button>}<button data-testid="send-button" className="send-button" type="submit" disabled={!canSend} aria-label={canSteer ? '补充指令' : state.busy ? '正在回复，请稍候' : '发送消息'} title={canSteer ? '补充当前任务 · Enter' : '发送消息 · Enter'}><ArrowUp size={19} strokeWidth={2.4} /></button></div></div>
              </form>
              </GlassSurface>
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
