import { useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronRight, Circle, CircleAlert, CircleCheck, CirclePause, LoaderCircle, Monitor, Settings2, Wrench } from 'lucide-react';
import type { ExecutionTrace, RequestContext } from '../bridge';

const STATUS = { running: '进行中', completed: '已完成', failed: '未完成', cancelled: '已停止', needs_user: '需要你接手', interrupted: '已中断' };
const ROLE: Record<string, string> = { system: '系统提示', developer: '应用规则', user: '用户', assistant: '助手', tool: '工具结果' };
const SOURCE: Record<string, string> = { events: '画面情境', screen_events: '屏幕情境', screen: '屏幕情境', camera: '摄像头情境', memories: '相关记忆', memory: '相关记忆', selection: '页面选区', selections: '页面选区', selection_contexts: '页面选区', images: '图片附件', summaries: '会话概况', summary: '会话概况' };
const number = (value: number) => new Intl.NumberFormat('zh-CN').format(value);

export function durationLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}分${seconds % 60 ? `${seconds % 60}秒` : ''}`;
}

export function offsetLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(seconds / 60);
  return `+${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function ContextDetails({ context, onSettings }: { context: RequestContext; onSettings?: () => void }) {
  const [open, setOpen] = useState(false);
  const references = Object.entries(context.source_counts).filter(([kind, count]) => count > 0 && !['system', 'history', 'current'].includes(kind));
  return <details className="request-context" data-testid="request-context" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary><ChevronRight size={14} aria-hidden="true" /><span>本次上下文</span><span className="context-preview">{context.messages.length}条消息{references.length ? ` · ${references.reduce((sum, [, count]) => sum + count, 0)}项参考` : ''}</span></summary>
    {open && <div className="context-content" data-testid="request-context-content">
      {onSettings && <button type="button" className="context-settings" data-testid="context-settings" onClick={onSettings}><Settings2 size={14} aria-hidden="true" />上下文设置</button>}
      <dl className="context-facts">
        <div><dt>聊天模型</dt><dd>{context.model || '当前配置模型'}</dd></div>
        {context.agent_model && <div><dt>任务模型</dt><dd>{context.agent_model}</dd></div>}
        {context.gui_model && <div><dt>电脑模型</dt><dd>{context.gui_model}</dd></div>}
        <div><dt>上下文策略</dt><dd>{context.policy === 'model' ? '跟随模型' : '手动兼容模式'}</dd></div>
        <div><dt>历史消息</dt><dd>{number(context.history_messages)}条</dd></div>
        <div><dt>估算输入</dt><dd>{number(context.estimated_input_tokens)} tokens</dd></div>
        <div><dt>模型窗口</dt><dd>{context.model_context_tokens ? `${number(context.model_context_tokens)} tokens` : '由模型服务管理'}</dd></div>
        <div><dt>输出长度</dt><dd>{context.output_tokens ? `${number(context.output_tokens)} tokens` : '由模型服务决定'}</dd></div>
      </dl>
      {references.length > 0 && <div className="context-sources" aria-label="本次携带的参考资料">{references.map(([kind, count]) => <span key={kind}>{SOURCE[kind] || '其他参考'} {number(count)}</span>)}</div>}
      <p className="context-note">以下为本次请求实际携带的文本，已隐藏凭据与图片数据。</p>
      {context.messages.length ? <ol className="context-messages">{context.messages.map((message, index) => <li key={`${index}-${message.role}`}>
        <span className="context-role">{index + 1} · {ROLE[message.role] || '消息'}</span><pre tabIndex={0}>{message.content}</pre>
      </li>)}</ol> : <p className="context-note">这条历史记录没有保存文本上下文。</p>}
    </div>}
  </details>;
}

export function ExecutionDetails({ execution, onContextSettings }: { execution: ExecutionTrace; onContextSettings?: () => void }) {
  const running = execution.state === 'running';
  const [open, setOpen] = useState(running);
  const [now, setNow] = useState(Date.now());
  const previousRun = useRef({ running, startedAt: execution.started_at });
  const id = useId();
  useEffect(() => {
    if (previousRun.current.running !== running || previousRun.current.startedAt !== execution.started_at) setOpen(running);
    previousRun.current = { running, startedAt: execution.started_at };
  }, [running, execution.started_at]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const started = Date.parse(execution.started_at);
  const elapsed = running && Number.isFinite(started) ? Math.max(execution.duration_ms, Math.max(0, now - started)) : execution.duration_ms;
  const operations = execution.events.filter((event) => event.kind === 'tool' || event.kind === 'computer').length;
  const summary = `${running ? '已用时' : '用时'} ${durationLabel(elapsed)}${operations ? ` · ${operations}项操作` : ''}`;
  return <div className={`execution-record execution-${execution.state}`} data-testid="execution-record" data-execution-state={execution.state}>
    <button type="button" className="execution-toggle" data-testid="execution-toggle" aria-expanded={open} aria-controls={id}
      aria-label={`执行过程，${summary}，${STATUS[execution.state]}`} onClick={() => setOpen((value) => !value)}>
      {running ? <LoaderCircle className="spin execution-status-icon" size={15} aria-hidden="true" />
        : execution.state === 'completed' ? <CircleCheck className="execution-status-icon" size={15} aria-hidden="true" />
        : ['failed', 'needs_user'].includes(execution.state) ? <CircleAlert className="execution-status-icon" size={15} aria-hidden="true" />
        : <CirclePause className="execution-status-icon" size={15} aria-hidden="true" />}
      <span>{summary}</span>
      {!running && execution.state !== 'completed' && <span className="execution-outcome">{STATUS[execution.state]}</span>}
      <ChevronRight className={open ? 'chevron-open' : ''} size={14} aria-hidden="true" />
    </button>
    {open && <section id={id} className="execution-content" aria-label="执行过程" data-testid="execution-content">
      <p className="execution-heading">执行过程<span>时间从任务开始计</span></p>
      {execution.events.length ? <ol className="execution-timeline">{execution.events.map((event) => <li key={event.id} className={`execution-event event-${event.status}`}>
        <span className="execution-marker" role="img" aria-label={event.status === 'completed' ? '已完成' : event.status === 'failed' ? '失败' : running ? '进行中' : '未完成'}>{event.status === 'running' && running ? <LoaderCircle size={13} className="spin" aria-hidden="true" /> : event.status === 'failed' ? <CircleAlert size={13} aria-hidden="true" /> : event.kind === 'computer' ? <Monitor size={13} aria-hidden="true" /> : event.kind === 'tool' ? <Wrench size={13} aria-hidden="true" /> : event.status === 'completed' ? <Check size={13} aria-hidden="true" /> : <Circle size={9} aria-hidden="true" />}</span>
        <div className="execution-event-body"><div className="execution-event-title"><span>{event.label}</span><time aria-label={`任务开始后 ${durationLabel(event.elapsed_ms)}`} title={`任务开始后 ${durationLabel(event.elapsed_ms)}`}>{offsetLabel(event.elapsed_ms)}</time></div>{event.detail && <p>{event.detail}</p>}</div>
      </li>)}</ol> : <p className="execution-empty">{running ? '正在准备本次请求…' : '这条记录没有保存详细操作。'}</p>}
      {execution.context && <ContextDetails context={execution.context} onSettings={onContextSettings} />}
    </section>}
  </div>;
}
