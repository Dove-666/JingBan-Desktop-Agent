import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { AppWindow, ArrowDownUp, ChevronRight, CircleAlert, CircleCheck, CirclePause, FileText,
  Globe2, Keyboard, ListChecks, LoaderCircle, Monitor, MousePointer2, Pencil, ScanEye, Search,
  SquareTerminal, Users, Wrench } from 'lucide-react';
import type { ExecutionEvent, ExecutionTrace } from '../bridge';

const STATUS = { running: '进行中', completed: '已完成', failed: '未完成', cancelled: '已停止', needs_user: '未完成', interrupted: '已中断' };
const RECENT_ROWS = 10;

export function durationLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}分${seconds % 60 ? `${seconds % 60}秒` : ''}`;
}

export function displayedReply(text: string, execution?: ExecutionTrace): string {
  // Keep the archive intact: a legacy unverified result is never relabelled as success.
  if (execution && ['needs_user', 'failed'].includes(execution.state) &&
      /^(?:任务尚未确认完成。|任务尚未通过验收：存在未完成步骤、工具失败或产物校验问题。)/.test(text)) {
    return '未完成：执行结果检查未通过。';
  }
  return text;
}

function operationDetail(event: ExecutionEvent): string {
  const detail = event.detail?.trim() ?? '';
  // Historic records have no richer evidence. Hide their counters without inventing actions.
  if (/^第\s*\d+\s*步$/.test(detail)) return '';
  return detail.replace(/^(?:文件|网站|应用|窗口|子任务|任务)[：:]\s*/, '');
}

function operationIcon(event: ExecutionEvent) {
  const label = event.label;
  if (/子任务|子智能体|子 Agent|协作|委派/.test(label)) return Users;
  if (/命令|终端|脚本|代码/.test(label)) return SquareTerminal;
  if (/网页|网站|联网/.test(label)) return Globe2;
  if (/搜索|查找|检索/.test(label)) return Search;
  if (/编辑|修改|写入|保存文件|创建文件/.test(label)) return Pencil;
  if (/文件|文档|图片|目录|读取/.test(label) && event.kind !== 'computer') return FileText;
  if (/计划|规划|步骤/.test(label)) return ListChecks;
  if (/点击|拖动|鼠标/.test(label)) return MousePointer2;
  if (/输入|按键|键盘|快捷键/.test(label)) return Keyboard;
  if (/滚动/.test(label)) return ArrowDownUp;
  if (/画面|截图|屏幕/.test(label)) return ScanEye;
  if (/窗口|应用|程序/.test(label)) return AppWindow;
  return event.kind === 'computer' ? Monitor : Wrench;
}

type ActivityRow = { id: string; events: ExecutionEvent[] };
function activityRows(events: ExecutionEvent[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  for (const event of events) {
    if (!['tool', 'computer', 'commentary'].includes(event.kind)) continue;
    const last = rows.at(-1);
    const previous = last?.events.at(-1);
    if (last && previous && event.kind !== 'commentary' && previous.kind === event.kind &&
        previous.status === 'completed' && event.status === 'completed' &&
        previous.label === event.label && operationDetail(previous) === operationDetail(event)) {
      last.events.push(event);
    } else rows.push({ id: event.id, events: [event] });
  }
  return rows;
}

function OperationLine({ event, running, children }: {
  event: ExecutionEvent; running: boolean; children?: React.ReactNode;
}) {
  const active = event.status === 'running' && running;
  const failed = event.status === 'failed';
  const status = failed ? '未完成' : active ? '进行中' : event.status === 'completed' ? '已完成' : '已停止';
  const Icon = operationIcon(event);
  const detail = operationDetail(event);
  return <div className={`execution-event event-${event.status}`} data-activity-id={event.id}
    title={`任务开始后 ${durationLabel(event.elapsed_ms)} · ${status}`}>
    <span className="execution-marker" role="img" aria-label={status}>
      {active ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Icon size={16} aria-hidden="true" />}
    </span>
    <div className="execution-event-title">
      <span className="execution-action">{event.label === '需要手动接手' ? '操作已暂停' : event.label}</span>
      {detail && <small className="execution-object">{detail}</small>}
      {failed && <small className="execution-event-status"><CircleAlert size={12} aria-hidden="true" />未完成</small>}
      {event.status === 'running' && !running && <small className="execution-event-status">已停止</small>}
      {children}
    </div>
  </div>;
}

function ActivityItem({ row, running }: { row: ActivityRow; running: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const event = row.events[0];
  if (event.kind === 'commentary') {
    return <li className="execution-commentary" data-testid="execution-commentary">{event.label}</li>;
  }
  if (row.events.length === 1) return <li><OperationLine event={event} running={running} /></li>;
  return <li className="execution-group">
    <button className="execution-group-toggle" type="button" aria-expanded={expanded} aria-controls={id}
      data-testid="execution-group-toggle" onClick={() => setExpanded(value => !value)}>
      <OperationLine event={event} running={running}>
        <small className="execution-repeat">{row.events.length} 次</small>
        <ChevronRight size={13} className={expanded ? 'chevron-open' : ''} aria-hidden="true" />
      </OperationLine>
    </button>
    {expanded && <ol id={id} className="execution-group-items" aria-label="重复操作记录">
      {row.events.map(item => <li key={item.id}><OperationLine event={item} running={false} /></li>)}
    </ol>}
  </li>;
}

export function ExecutionDetails({ execution }: { execution: ExecutionTrace }) {
  const running = execution.state === 'running';
  const [open, setOpen] = useState(running);
  const [olderOpen, setOlderOpen] = useState(false);
  const [now, setNow] = useState(Date.now());
  const previousRun = useRef({ running, startedAt: execution.started_at });
  const id = useId();
  const olderId = useId();
  useEffect(() => {
    if (previousRun.current.running !== running || previousRun.current.startedAt !== execution.started_at) setOpen(running);
    if (previousRun.current.startedAt !== execution.started_at) setOlderOpen(false);
    previousRun.current = { running, startedAt: execution.started_at };
  }, [running, execution.started_at]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const started = Date.parse(execution.started_at);
  const elapsed = running && Number.isFinite(started) ? Math.max(execution.duration_ms, Math.max(0, now - started)) : execution.duration_ms;
  const rows = useMemo(() => activityRows(execution.events), [execution.events]);
  const operations = execution.events.filter(event => event.kind === 'tool' || event.kind === 'computer').length;
  const older = rows.slice(0, Math.max(0, rows.length - RECENT_ROWS));
  const recent = rows.slice(-RECENT_ROWS);
  const olderActive = running ? older.filter(row => row.events.some(event => event.status === 'running')) : [];
  const olderHidden = older.filter(row => !olderActive.includes(row));
  const olderCount = olderHidden.reduce((count, row) => count + row.events.length, 0);
  const olderFailed = olderHidden.reduce((count, row) => count + row.events.filter(event => event.status === 'failed').length, 0);
  const summary = `${running ? '已用时' : '用时'} ${durationLabel(elapsed)}${operations ? ` · ${operations}项操作` : ''}`;
  return <div className={`execution-record execution-${execution.state}`} data-testid="execution-record" data-execution-state={execution.state}>
    <button type="button" className="execution-toggle" data-testid="execution-toggle" aria-expanded={open} aria-controls={id}
      aria-label={`执行过程，${summary}，${STATUS[execution.state]}`} onClick={() => setOpen(value => !value)}>
      {running ? <LoaderCircle className="spin execution-status-icon" size={15} aria-hidden="true" />
        : execution.state === 'completed' ? <CircleCheck className="execution-status-icon" size={15} aria-hidden="true" />
        : ['failed', 'needs_user'].includes(execution.state) ? <CircleAlert className="execution-status-icon" size={15} aria-hidden="true" />
        : <CirclePause className="execution-status-icon" size={15} aria-hidden="true" />}
      <span>{summary}</span>
      {!running && execution.state !== 'completed' && <span className="execution-outcome">{STATUS[execution.state]}</span>}
      <ChevronRight className={open ? 'chevron-open' : ''} size={14} aria-hidden="true" />
    </button>
    {open && <section id={id} className="execution-content" aria-label="执行过程" data-testid="execution-content">
      {rows.length ? <>
        {olderHidden.length > 0 && <button type="button" className="execution-earlier" data-testid="execution-earlier-toggle"
          aria-expanded={olderOpen} aria-controls={olderId} onClick={() => setOlderOpen(value => !value)}>
          <ChevronRight size={13} className={olderOpen ? 'chevron-open' : ''} aria-hidden="true" />
          {olderOpen ? '收起较早活动' : `较早的 ${olderCount} 项活动`}
          {olderFailed > 0 && <span className="execution-earlier-failed"><CircleAlert size={12} aria-hidden="true" />{olderFailed} 项未完成</span>}
        </button>}
        {olderOpen && older.length > 0 && <ol id={olderId} className="execution-activities execution-older" aria-label="较早活动">
          {older.map(row => <ActivityItem key={row.id} row={row} running={running} />)}
        </ol>}
        {!olderOpen && olderActive.length > 0 && <ol className="execution-activities" aria-label="仍在进行的活动">
          {olderActive.map(row => <ActivityItem key={row.id} row={row} running={running} />)}
        </ol>}
        <ol className="execution-activities" aria-label="最近活动">
          {recent.map(row => <ActivityItem key={row.id} row={row} running={running} />)}
        </ol>
      </> : <p className="execution-empty">{running ? '正在分析任务…' : '没有执行操作。'}</p>}
    </section>}
  </div>;
}
