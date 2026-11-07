import { useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronRight, Circle, CircleAlert, CircleCheck, CirclePause, LoaderCircle, Monitor, Wrench } from 'lucide-react';
import type { ExecutionTrace } from '../bridge';

const STATUS = { running: '进行中', completed: '已完成', failed: '未完成', cancelled: '已停止', needs_user: '未完成', interrupted: '已中断' };

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

export function displayedReply(text: string, execution?: ExecutionTrace): string {
  // Keep the archive intact; removing legacy boilerplate must not turn an
  // old unverified success paragraph into an apparently completed result.
  if (execution && ['needs_user', 'failed'].includes(execution.state) &&
      /^(?:任务尚未确认完成。|任务尚未通过验收：存在未完成步骤、工具失败或产物校验问题。)/.test(text)) {
    return '未完成：执行结果检查未通过。';
  }
  return text;
}

export function ExecutionDetails({ execution }: { execution: ExecutionTrace }) {
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
  const events = execution.events.filter((event) => event.kind === 'tool' || event.kind === 'computer');
  const operations = events.length;
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
      <p className="execution-heading">执行过程</p>
      {events.length ? <ol className="execution-timeline">{events.map((event) => <li key={event.id} className={`execution-event event-${event.status}`}>
        <span className="execution-marker" role="img" aria-label={event.status === 'completed' ? '已完成' : event.status === 'failed' ? '失败' : running ? '进行中' : '未完成'}>{event.status === 'running' && running ? <LoaderCircle size={13} className="spin" aria-hidden="true" /> : event.status === 'failed' ? <CircleAlert size={13} aria-hidden="true" /> : event.kind === 'computer' ? <Monitor size={13} aria-hidden="true" /> : event.kind === 'tool' ? <Wrench size={13} aria-hidden="true" /> : event.status === 'completed' ? <Check size={13} aria-hidden="true" /> : <Circle size={9} aria-hidden="true" />}</span>
        <div className="execution-event-body"><div className="execution-event-title"><span>{event.label === '需要手动接手' ? '操作已暂停' : event.label}</span><time aria-label={`任务开始后 ${durationLabel(event.elapsed_ms)}`} title={`任务开始后 ${durationLabel(event.elapsed_ms)}`}>{offsetLabel(event.elapsed_ms)}</time></div>{event.detail && <p>{event.detail}</p>}</div>
      </li>)}</ol> : <p className="execution-empty">{running ? '正在分析任务…' : '没有执行操作。'}</p>}
    </section>}
  </div>;
}
