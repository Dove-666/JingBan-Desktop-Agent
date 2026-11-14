import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Hand, LoaderCircle, ShieldAlert, ShieldCheck } from 'lucide-react';
import type { AgentApprovalMode, Dispatch } from '../bridge';
import './agent-approval.css';

const OPTIONS = [
  { value: 'ask', label: '请求批准', detail: '联网、修改工作区外文件及执行命令前询问', Icon: Hand },
  { value: 'auto', label: '帮我批准', detail: '由模型审查，发现风险时询问', Icon: ShieldCheck },
  { value: 'full', label: '完全访问权限', detail: '自动执行已启用的工具，不逐次询问', Icon: ShieldAlert },
] as const;

export function AgentApprovalMenu({ mode, disabled, conversationId, dispatch }: {
  mode: AgentApprovalMode; disabled: boolean; conversationId: string; dispatch: Dispatch;
}) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const options = useRef<(HTMLButtonElement | null)[]>([]);
  const request = useRef(0);
  const id = useId();
  const selected = OPTIONS.find(option => option.value === mode) ?? OPTIONS[1];
  const Icon = selected.Icon;

  useEffect(() => {
    request.current += 1;
    setSaving(false);
    setError('');
    setOpen(false);
    return () => { request.current += 1; };
  }, [conversationId]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);

  useLayoutEffect(() => {
    if (!open || !trigger.current || !menu.current) return;
    const anchor = trigger.current.getBoundingClientRect();
    const bounds = menu.current.getBoundingClientRect();
    const above = anchor.top - bounds.height - 8;
    setPosition({
      left: Math.max(12, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 12)),
      top: Math.max(12, Math.min(above >= 12 ? above : anchor.bottom + 8,
        window.innerHeight - bounds.height - 12)),
    });
    options.current[OPTIONS.findIndex(option => option.value === mode)]?.focus({ preventScroll: true });
  }, [open, mode]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !trigger.current?.contains(event.target) && !menu.current?.contains(event.target)) setOpen(false);
    };
    const resize = () => setOpen(false);
    document.addEventListener('pointerdown', outside, true);
    window.addEventListener('resize', resize);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      window.removeEventListener('resize', resize);
    };
  }, [open]);

  const close = () => { setOpen(false); trigger.current?.focus({ preventScroll: true }); };
  const choose = (value: AgentApprovalMode) => {
    close();
    if (disabled || saving || value === mode) return;
    const serial = ++request.current;
    setError('');
    setSaving(true);
    const accepted = dispatch('setAgentApprovalMode', { conversationId, mode: value }, result => {
      if (request.current !== serial) return;
      setSaving(false);
      if (!result.ok) setError('权限未能保存，请重试。');
    });
    if (!accepted && request.current === serial) {
      setSaving(false);
      setError('权限未能保存，请重试。');
    }
  };
  const navigate = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
    if (event.key === 'Tab') { close(); return; }
    const index = options.current.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'ArrowDown' ? (index + 1) % OPTIONS.length
      : event.key === 'ArrowUp' ? (index + OPTIONS.length - 1) % OPTIONS.length
      : event.key === 'Home' ? 0 : event.key === 'End' ? OPTIONS.length - 1 : -1;
    if (next >= 0) { event.preventDefault(); options.current[next]?.focus(); }
  };

  return <div className="agent-approval-control">
    <button type="button" ref={trigger} className="agent-approval-trigger" data-mode={mode}
      data-testid="agent-approval-trigger" aria-label={`Agent 操作权限：${selected.label}`}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined}
      title={disabled ? '任务结束后可修改操作权限' : selected.detail + (mode === 'auto' ? '；会增加模型请求' : '')} disabled={disabled || saving}
      onClick={() => { setError(''); setOpen(value => !value); }}>
      {saving ? <LoaderCircle size={15} className="spin" aria-hidden="true" /> : <Icon size={15} aria-hidden="true" />}
      <span>{selected.label}</span><ChevronDown size={13} aria-hidden="true" />
    </button>
    {error && <span className="agent-approval-error" role="alert">{error}</span>}
    {open && createPortal(<div ref={menu} id={id} className="agent-approval-menu" role="menu"
      aria-labelledby={`${id}-title`} data-testid="agent-approval-menu" style={position} onKeyDown={navigate}>
      <div className="agent-approval-title" id={`${id}-title`}>Agent 操作权限</div>
      {OPTIONS.map(({ value, label, detail, Icon: OptionIcon }, index) => <button key={value}
        ref={element => { options.current[index] = element; }} type="button" role="menuitemradio"
        aria-checked={mode === value} data-testid={`agent-approval-${value}`} data-mode={value}
        title={value === 'auto' ? '模型审查会增加模型请求' : undefined}
        className="agent-approval-option" onClick={() => choose(value)}>
        <OptionIcon size={18} aria-hidden="true" />
        <span className="agent-approval-copy"><strong>{label}</strong><span>{detail}</span></span>
        <Check size={16} className="agent-approval-check" aria-hidden="true" />
      </button>)}
    </div>, document.body)}
  </div>;
}
