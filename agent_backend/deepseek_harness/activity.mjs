/** Public progress is derived from real DSH events, never raw arguments/reasoning. */
import { createHash } from 'node:crypto';
import { operationSubject, parsedArguments, publicCommentary, selectedApplication } from './activity-subject.mjs';

function identity(value) { return typeof value === 'string' && value.length > 0 && value.length <= 512; }
function eventId(value) { return `dsh-${createHash('sha256').update(value).digest('hex')}`; }

export class ActivityProjection {
  constructor(id, emit) {
    this.id = id;
    this.emit = emit;
    this.calls = new Map();
    this.artifacts = new Map();
    this.tools = new Set();
    this.step = 0;
    this.applications = new Map();
    this.commentary = new Map();
  }

  notification(notification) {
    if (['subagent.started', 'subagent.finished'].includes(notification.method)) {
      const params = notification.params;
      if (!identity(params?.parentSessionId) || !identity(params?.childSessionId)) return;
      const starting = notification.method === 'subagent.started';
      const action = starting ? 'start' : params.stopReason === 'aborted' ? 'stop'
        : params.status === 'ok' && params.stopReason === 'completed' ? 'finish' : 'fail';
      this.emit({ type: 'activity', id: this.id, name: 'subagent_activity', kind: 'tool',
        event_id: eventId(`subagent:${params.parentSessionId}:${params.childSessionId}`),
        subject: { kind: 'agent', action }, status: starting ? 'running' : action === 'finish' ? 'completed' : 'failed' });
      return;
    }
    if (notification.method !== 'session.event') return;
    const sessionId = notification.params?.sessionId;
    if (!identity(sessionId)) return;
    const event = notification.params?.event;
    const data = event?.data;
    if (!data || typeof data !== 'object') return;
    if (event.type === 'request/header') {
      for (const tool of data.header?.tools ?? []) if (typeof tool?.name === 'string') this.tools.add(tool.name);
    } else if (event.type === 'step/start') {
      this.commentary.delete(sessionId);
      this.step += 1;
      this.emit({ type: 'progress', id: this.id, phase: 'planning', step: this.step });
    } else if (event.type === 'assistant/message') {
      this.commentary.delete(sessionId);
      const message = data.message;
      // Official TextBlock is user-visible; ReasoningBlock and raw stream are not.
      // Require tool calls in this settled message and an actual matching dispatch
      // before publishing, so final answers and interrupted output are not repeated.
      if (data.interrupted || message?.role !== 'assistant' || !Array.isArray(message.content)) return;
      const calls = message.content.filter(block => block?.type === 'tool-call' && identity(block.id)).map(block => block.id);
      if (!calls.length) return;
      const text = publicCommentary(message.content.filter(block => block?.type === 'text' && typeof block.text === 'string')
        .map(block => block.text).join('\n\n'));
      if (text) this.commentary.set(sessionId, { text, calls: new Set(calls), turn: data.turn, step: data.step });
    } else if (event.type === 'tool/call' && typeof data.name === 'string' && typeof data.callId === 'string') {
      const name = /^[A-Za-z0-9_.-]{1,100}$/.test(data.name) ? data.name : 'tool';
      const event_id = eventId(`${sessionId}:${data.callId}`);
      const note = this.commentary.get(sessionId);
      if (note?.calls.has(data.callId) && note.turn === data.turn && note.step === data.step) {
        this.commentary.delete(sessionId);
        this.emit({ type: 'activity', id: this.id, name: 'assistant_commentary', kind: 'commentary',
          event_id: eventId(`commentary:${sessionId}:${data.callId}`), status: 'completed', text: note.text });
      }
      const args = parsedArguments(data.arguments);
      const subject = operationSubject(name, args, name === 'cua' && identity(args?.params?.appHandle)
        ? this.applications.get(`${sessionId}:${args.params.appHandle}`) : undefined);
      const call = { name, event_id,
        kind: name === 'cua' || name === 'cua_run_code' ? 'computer' : 'tool', step: this.step,
        ...subject ? { subject } : {} };
      this.calls.set(`${sessionId}:${data.callId}`, { call, appSelection: name === 'cua' && args?.method === 'getApp' });
      this.emit({ type: 'activity', id: this.id, ...call, status: 'running' });
    } else if (event.type === 'tool/result') {
      const message = data.message;
      const callId = message?.toolCallId ?? message?.callId;
      const pending = this.calls.get(`${sessionId}:${callId}`);
      if (pending) {
        this.calls.delete(`${sessionId}:${callId}`);
        const { call } = pending;
        const application = pending.appSelection ? selectedApplication(message) : null;
        if (application) {
          if (this.applications.size >= 256) this.applications.delete(this.applications.keys().next().value);
          this.applications.set(`${sessionId}:${application.handle}`, application.name);
          if (call.subject) call.subject = { ...call.subject, name: application.name };
        }
        this.emit({ type: 'activity', id: this.id, ...call, status: message.isError ? 'failed' : 'completed' });
      }
    } else if (event.type === 'compaction/start') {
      this.commentary.delete(sessionId);
      this.emit({ type: 'progress', id: this.id, phase: 'planning' });
    } else if (event.type === 'step/end' || event.type === 'turn/end') {
      this.commentary.delete(sessionId);
    } else if (event.type === 'deliverables/presented') {
      for (const file of data.files ?? []) {
        if (typeof file?.path === 'string' && file.path.length <= 4096) this.artifacts.set(file.path, { path: file.path });
      }
    }
  }
}
