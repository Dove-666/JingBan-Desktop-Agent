import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowUpRight } from 'lucide-react';
import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import type { ChatMessage } from '../bridge';
import './ConversationRail.css';

export interface ConversationRailProps {
  messages: ChatMessage[];
  conversationId: string;
  viewportRef: RefObject<HTMLDivElement | null>;
  contentRef?: RefObject<HTMLDivElement | null>;
  disabled?: boolean;
  bottomInset?: number;
  onNavigate?: (id: string) => void;
}

type Position = { index: number; top: number; bottom: number; node: HTMLElement };
type Geometry = { positions: Position[]; viewportHeight: number; maxScroll: number; top: number; height: number; right: number };
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(maximum, value));
const positionRatio = (index: number, length: number) => length > 1 ? index / (length - 1) : .5;

/** Small, local text previews. Markdown and provider HTML remain plain text. */
function previewText(message: ChatMessage) {
  const text = message.content.slice(0, 2400)
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/gm, '')
    .replace(/```[^\n]*\n?/g, '')
    .replace(/\*\*|__|`/g, '')
    .trim();
  if (text) return text;
  if (message.metadata?.image_id || message.metadata?.image_unavailable) return '图片消息';
  if (message.metadata?.execution) return '电脑任务记录';
  return '暂无文字内容';
}

/** Pair an answer with its question, without creating or storing a summary. */
function previewContent(entries: ChatMessage[], index: number) {
  const message = entries[index];
  let question = index;
  if (message.role === 'assistant') {
    while (question >= 0 && entries[question].role !== 'user') question--;
  }
  const title = question >= 0 ? previewText(entries[question]).split('\n')[0] : '对话片段';
  const answer = message.role === 'user' && entries[index + 1]?.role === 'assistant' ? entries[index + 1] : message;
  // Blank lines must not consume a short preview. Content stays escaped text.
  const excerpt = previewText(answer).replace(/\s+/g, ' ');
  return { title, excerpt };
}

export const ConversationRail = memo(function ConversationRail({ messages, conversationId, viewportRef,
  contentRef, disabled = false, bottomInset = 140, onNavigate }: ConversationRailProps) {
  const entries = useMemo(() => messages.filter((message) => Boolean(message.id)), [messages]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [previewIndex, setPreviewIndex] = useState<number | null>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [railHeight, setRailHeight] = useState(320);
  const [cardPosition, setCardPosition] = useState({ left: 0, top: 0 });
  const rail = useRef<HTMLDivElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const geometry = useRef<Geometry>({ positions: [], viewportHeight: 0, maxScroll: 0, top: 0, height: 0, right: 0 });
  const active = useRef(0);
  const keyboardIndex = useRef<number | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const tooltipId = useId();
  const available = !disabled && entries.length > 0;
  const interactive = available && overflowing;
  const safeActiveIndex = clamp(activeIndex, 0, Math.max(0, entries.length - 1));
  const focusedMessage = entries[safeActiveIndex];
  const previewMessage = previewIndex === null ? undefined : entries[previewIndex];
  const preview = useMemo(() => previewIndex !== null && previewMessage
    ? previewContent(entries, previewIndex) : null, [entries, previewIndex, previewMessage]);

  const cancelClose = useCallback(() => clearTimeout(closeTimer.current), []);
  const hidePreview = useCallback(() => {
    cancelClose();
    setPreviewIndex(null);
  }, [cancelClose]);
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = setTimeout(() => setPreviewIndex(null), 160);
  }, [cancelClose]);

  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useLayoutEffect(() => {
    cancelClose();
    setPreviewIndex(null);
    setOverflowing(false);
    keyboardIndex.current = null;
  }, [conversationId, disabled, cancelClose]);

  useEffect(() => {
    if (!previewMessage || !interactive) return;
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      hidePreview();
    };
    // Capture before the page's own Escape handler, including when the mouse
    // opened a preview while the editor retained keyboard focus.
    window.addEventListener('keydown', onEscape, true);
    return () => window.removeEventListener('keydown', onEscape, true);
  }, [previewMessage, interactive, hidePreview]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const railNode = rail.current;
    if (!available || !viewport || !railNode) {
      setOverflowing(false);
      return;
    }
    let frame = 0;
    let needsMeasure = true;
    let disposed = false;
    const updateActive = () => {
      const cache = geometry.current;
      if (!cache.positions.length) return;
      const scrollTop = viewport.scrollTop;
      const readingPoint = scrollTop + Math.min(cache.viewportHeight * .25, 110);
      let next = cache.positions[0].index;
      if (cache.maxScroll > 1 && scrollTop >= cache.maxScroll - 3) {
        next = cache.positions[cache.positions.length - 1].index;
      } else {
        let low = 0, high = cache.positions.length - 1;
        while (low <= high) {
          const middle = (low + high) >> 1;
          if (cache.positions[middle].top <= readingPoint) {
            next = cache.positions[middle].index;
            low = middle + 1;
          } else high = middle - 1;
        }
      }
      active.current = next;
      setActiveIndex((previous) => previous === next ? previous : next);
    };
    const measure = () => {
      const viewportBounds = viewport.getBoundingClientRect();
      const railBounds = railNode.getBoundingClientRect();
      const nodes = new Map([...viewport.querySelectorAll<HTMLElement>('.message[data-message-id]')]
        .map((node) => [node.dataset.messageId, node]));
      geometry.current = {
        positions: entries.flatMap((entry, index) => {
          const node = nodes.get(entry.id);
          if (!node) return [];
          const bounds = node.getBoundingClientRect();
          return [{ index, node, top: bounds.top - viewportBounds.top + viewport.scrollTop,
            bottom: bounds.bottom - viewportBounds.top + viewport.scrollTop }];
        }),
        viewportHeight: viewport.clientHeight,
        maxScroll: Math.max(0, viewport.scrollHeight - viewport.clientHeight),
        top: railBounds.top, height: railBounds.height, right: railBounds.right,
      };
      const hasOverflow = geometry.current.maxScroll > 1;
      setOverflowing((previous) => previous === hasOverflow ? previous : hasOverflow);
      if (!hasOverflow) {
        if (document.activeElement === railNode) railNode.blur();
        hidePreview();
        keyboardIndex.current = null;
      }
      setRailHeight((height) => Math.abs(height - railBounds.height) < 1 ? height : railBounds.height);
    };
    const schedule = (measureAgain = false) => {
      needsMeasure ||= measureAgain;
      if (frame || disposed) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (disposed) return;
        if (needsMeasure) { measure(); needsMeasure = false; }
        updateActive();
      });
    };
    const onScroll = () => schedule();
    const onResize = () => schedule(true);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(onResize);
    observer?.observe(viewport);
    observer?.observe(railNode);
    if (contentRef?.current) observer?.observe(contentRef.current);
    viewport.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onResize);
    schedule(true);
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer?.disconnect();
      viewport.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onResize);
    };
  }, [available, conversationId, entries, viewportRef, contentRef, bottomInset, hidePreview]);

  const reveal = useCallback((index: number) => {
    cancelClose();
    setPreviewIndex(clamp(index, 0, entries.length - 1));
  }, [entries.length, cancelClose]);

  useLayoutEffect(() => {
    if (!previewMessage || previewIndex === null || !card.current) return;
    const place = () => {
      const bounds = rail.current?.getBoundingClientRect();
      const previewNode = card.current;
      if (!bounds || !previewNode) return;
      const width = previewNode.offsetWidth;
      const height = previewNode.offsetHeight;
      const point = bounds.top + positionRatio(previewIndex, entries.length) * bounds.height;
      const left = clamp(bounds.right + 12, 8, Math.max(8, window.innerWidth - width - 8));
      const top = clamp(point - height / 2, 8, Math.max(8, window.innerHeight - height - 8));
      setCardPosition((previous) => previous.left === left && previous.top === top ? previous : { left, top });
    };
    place();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
    observer?.observe(card.current);
    if (rail.current) observer?.observe(rail.current);
    if (viewportRef.current) observer?.observe(viewportRef.current);
    window.addEventListener('resize', place);
    return () => { observer?.disconnect(); window.removeEventListener('resize', place); };
  }, [previewIndex, previewMessage, entries.length, railHeight, bottomInset, viewportRef]);

  const navigate = useCallback((index: number) => {
    const entry = entries[index];
    if (!entry) return;
    if (onNavigate) onNavigate(entry.id);
    else {
      const target = geometry.current.positions.find((position) => position.index === index)?.node;
      target?.scrollIntoView({ block: 'start', behavior: 'instant' });
    }
    keyboardIndex.current = index;
  }, [entries, onNavigate]);

  const pointerIndex = (event: PointerEvent<HTMLDivElement>) => {
    const bounds = rail.current?.getBoundingClientRect();
    if (!bounds || bounds.height <= 0) return 0;
    return Math.round(clamp((event.clientY - bounds.top) / bounds.height, 0, 1) * Math.max(0, entries.length - 1));
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      hidePreview();
      return;
    }
    const current = keyboardIndex.current ?? active.current;
    let target = current;
    if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') target--;
    else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') target++;
    else if (event.key === 'PageUp') target -= 10;
    else if (event.key === 'PageDown') target += 10;
    else if (event.key === 'Home') target = 0;
    else if (event.key === 'End') target = entries.length - 1;
    else if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    event.stopPropagation();
    target = clamp(target, 0, entries.length - 1);
    navigate(target);
    reveal(target);
  };

  const ticks = useMemo(() => {
    const count = Math.min(entries.length, 72, Math.max(2, Math.floor(railHeight / 9)));
    return Array.from({ length: count }, (_, index) => count === 1 ? 0 : Math.round(index * (entries.length - 1) / (count - 1)));
  }, [entries.length, railHeight]);

  const onWheel = useCallback((event: globalThis.WheelEvent) => {
    const viewport = viewportRef.current;
    // The rail is a sibling of the scroller. Forward wheel input exactly once,
    // while preserving browser/system zoom gestures.
    if (!viewport || event.ctrlKey || event.metaKey) return;
    const unit = event.deltaMode === 1 ? 20 : event.deltaMode === 2 ? viewport.clientHeight : 1;
    viewport.scrollBy({ top: event.deltaY * unit, left: event.deltaX * unit, behavior: 'instant' });
    event.preventDefault();
    event.stopPropagation();
    hidePreview();
  }, [viewportRef, hidePreview]);
  useEffect(() => {
    const node = rail.current;
    if (!interactive || !node) return;
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  }, [interactive, onWheel]);

  if (!available || !focusedMessage) return null;
  const lensIndex = previewIndex ?? safeActiveIndex;
  const lensPosition = positionRatio(lensIndex, entries.length);
  return <>
    <div ref={rail} className="conversation-rail conversation-rail-track" data-testid="conversation-rail" role="slider"
      tabIndex={interactive ? 0 : -1} aria-hidden={!interactive || undefined} data-overflow={overflowing}
      aria-label="对话位置导航" aria-orientation="vertical" aria-valuemin={1}
      aria-valuemax={entries.length} aria-valuenow={safeActiveIndex + 1}
      aria-valuetext={`第 ${safeActiveIndex + 1} 条，${focusedMessage.role === 'user' ? '你的消息' : '鲸鱼娘的回复'}`}
      aria-describedby={previewMessage ? tooltipId : undefined} data-active-message-id={focusedMessage.id}
      style={{ bottom: Math.max(0, bottomInset) } as CSSProperties} data-previewing={previewIndex !== null}
      onPointerMove={(event) => { keyboardIndex.current = null; reveal(pointerIndex(event)); }}
      onPointerLeave={scheduleClose} onFocus={() => { keyboardIndex.current = null; reveal(active.current); }} onBlur={scheduleClose}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        rail.current?.focus({ preventScroll: true });
        const index = pointerIndex(event);
        navigate(index);
        reveal(index);
      }} onKeyDown={onKeyDown}>
      {ticks.map((index) => {
        const ratio = positionRatio(index, entries.length);
        const distance = Math.abs(ratio - lensPosition) * railHeight;
        const influence = previewIndex === null ? 0 : Math.exp(-.5 * (distance / 16) ** 2);
        return <span key={entries[index].id} aria-hidden="true" data-testid="conversation-rail-tick"
          data-rail-message-id={entries[index].id} data-index={index} className="conversation-rail-tick"
          style={{ top: `${ratio * 100}%`, '--tick-scale': 1 + influence * 2.3 } as CSSProperties} />;
      })}
      {previewIndex !== null && previewIndex !== safeActiveIndex && <span aria-hidden="true" className="conversation-rail-hover"
        style={{ top: `${positionRatio(previewIndex, entries.length) * 100}%` }} />}
      <span aria-hidden="true" data-testid="conversation-rail-active" className="conversation-rail-active"
        style={{ top: `${positionRatio(safeActiveIndex, entries.length) * 100}%`,
          opacity: previewIndex !== null && previewIndex !== safeActiveIndex ? 0 : undefined }} />
      <span aria-hidden="true" className="conversation-rail-focus"
        style={{ top: `${lensPosition * 100}%` }} />
    </div>
    {interactive && previewMessage && createPortal(<div ref={card} id={tooltipId} role="region" aria-label="对话预览"
      className="conversation-rail-preview" data-testid="conversation-rail-preview" data-preview-message-id={previewMessage.id}
      data-rail-message-id={previewMessage.id} data-rail-index={previewIndex}
      style={cardPosition} onPointerEnter={cancelClose} onPointerLeave={scheduleClose}>
      <button type="button" className="conversation-rail-preview-jump"
        aria-label={`跳转到第 ${(previewIndex ?? 0) + 1} 条消息`}
        onFocus={cancelClose} onBlur={scheduleClose}
        onClick={() => {
          rail.current?.focus({ preventScroll: true });
          if (previewIndex !== null) navigate(previewIndex);
          hidePreview();
        }}>
        <span className="conversation-rail-preview-heading"><span className="conversation-rail-preview-title">{preview?.title}</span>
          <ArrowUpRight size={14} strokeWidth={1.5} aria-hidden="true" /></span>
        <span className="conversation-rail-preview-excerpt">{preview?.excerpt}</span>
      </button>
    </div>, document.body)}
  </>;
});
