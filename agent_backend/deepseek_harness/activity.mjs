/** Public progress is derived from real DSH events, never raw arguments/reasoning. */
import { createHash } from 'node:crypto';
export class ActivityProjection {
  constructor(id, emit) {
    this.id = id;
    this.emit = emit;
    this.calls = new Map();
    this.artifacts = new Map();
    this.tools = new Set();
    this.step = 0;
  }

  notification(notification) {
    if (notification.method === 'subagent.started') {
      this.emit({ type: 'progress', id: this.id, phase: 'planning' });
      return;
    }
    if (notification.method !== 'session.event') return;
    const event = notification.params?.event;
    const data = event?.data;
    if (!data || typeof data !== 'object') return;
    if (event.type === 'request/header') {
      for (const tool of data.header?.tools ?? []) if (typeof tool?.name === 'string') this.tools.add(tool.name);
    } else if (event.type === 'step/start') {
      this.step += 1;
      this.emit({ type: 'progress', id: this.id, phase: 'planning', step: this.step });
    } else if (event.type === 'tool/call' && typeof data.name === 'string' && typeof data.callId === 'string') {
      const name = /^[A-Za-z0-9_.-]{1,100}$/.test(data.name) ? data.name : 'tool';
      const event_id = `dsh-${createHash('sha256').update(`${notification.params.sessionId}:${data.callId}`).digest('hex')}`;
      const call = { name, event_id,
        kind: name === 'cua' || name === 'cua_run_code' ? 'computer' : 'tool', step: this.step };
      this.calls.set(`${notification.params.sessionId}:${data.callId}`, call);
      this.emit({ type: 'activity', id: this.id, ...call, status: 'running' });
    } else if (event.type === 'tool/result') {
      const message = data.message;
      const callId = message?.toolCallId ?? message?.callId;
      const call = this.calls.get(`${notification.params.sessionId}:${callId}`);
      if (call) {
        this.calls.delete(`${notification.params.sessionId}:${callId}`);
        this.emit({ type: 'activity', id: this.id, ...call, status: message.isError ? 'failed' : 'completed' });
      }
    } else if (event.type === 'compaction/start') {
      this.emit({ type: 'progress', id: this.id, phase: 'planning' });
    } else if (event.type === 'deliverables/presented') {
      for (const file of data.files ?? []) {
        if (typeof file?.path === 'string' && file.path.length <= 4096) this.artifacts.set(file.path, { path: file.path });
      }
    }
  }
}
