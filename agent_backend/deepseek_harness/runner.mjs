/** A transport adapter around the upstream SDK. DSH owns the Agent loop and tools. */
import { mkdir, writeFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { ActivityProjection } from './activity.mjs';
import { snapshotPresentedFiles } from './artifacts.mjs';
import { createNativeReceipt, verifyNativeReceipt } from './native-receipt.mjs';
import { assertRuntimeSupported } from './runtime.mjs';
import { launchConfiguration, promptBlocks, runtimeEnvironment, PROVIDER, validateRun, textLength } from './configuration.mjs';

const SAFE_TOOL = /^[A-Za-z0-9_.-]{1,100}$/;

function failure(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

export function publicErrorCode(error) {
  const code = error?.code ?? error?.message;
  if (['INVALID_REQUEST', 'INVALID_CONFIG', 'INVALID_SESSION', 'INVALID_IMAGE', 'CANCELLED',
    'COMPUTER_HELPER_MISSING', 'COMPUTER_PROVIDER_MISSING', 'COMPUTER_PLATFORM_UNSUPPORTED',
    'COMPUTER_ARCHITECTURE_UNSUPPORTED', 'DSH_SESSION_UNAVAILABLE', 'DSH_MODEL_ERROR',
    'DSH_RUNTIME_MISSING', 'DSH_RUNTIME_UNSUPPORTED', 'DSH_EXECUTION_FAILED', 'DSH_STOP_UNCONFIRMED'].includes(code)) return code;
  if (error?.code === 'ERR_MODULE_NOT_FOUND') return 'DSH_RUNTIME_MISSING';
  return 'DSH_EXECUTION_FAILED';
}

export class HarnessRunner {
  constructor({ emit, Harness, computerOptions } = {}) {
    this.emit = emit ?? (() => {});
    this.Harness = Harness;
    this.computerOptions = computerOptions;
    this.cancelled = false;
    this.pending = new Map();
    this.questionQueue = Promise.resolve();
    this.steers = [];
    this.canSteer = false;
    this.shutdownConfirmed = false;
    this.stoppedEmitted = false;
  }

  async control(frame) {
    if (!frame || typeof frame !== 'object') return;
    if (frame.id && this.request && frame.id !== this.request.id) return;
    if (frame.type === 'cancel') return this.cancel();
    if (frame.type === 'interaction_reply') {
      const pending = this.pending.get(frame.interaction_id);
      if (pending) {
        this.pending.delete(frame.interaction_id);
        pending(frame.answer ?? null);
      }
    } else if (frame.type === 'steer') {
      if (typeof frame.text !== 'string' || !frame.text.trim() || textLength(frame.text) > 12000) throw failure('INVALID_REQUEST');
      this.steers.push(frame.text);
      await this.flushSteers();
    }
  }

  async flushSteers() {
    if (!this.canSteer || this.cancelled || !this.session || !this.harness) return;
    for (const text of this.steers.splice(0)) {
      await this.harness.client.prompt(this.session.id, [{ type: 'text', text }]);
    }
  }

  async cancel() {
    this.cancelled = true;
    this.canSteer = false;
    for (const resolve of this.pending.values()) resolve(null);
    this.pending.clear();
    if (this.harness) await this.close();
    else this.emitStopped(true); // run checks cancelled again after every pre-start await.
  }

  close() {
    this.closing ??= this.performClose();
    return this.closing;
  }

  emitStopped(confirmed) {
    this.shutdownConfirmed = confirmed;
    if (this.stoppedEmitted || !this.request) return;
    this.stoppedEmitted = true;
    this.emit({ type: 'stopped', id: this.request.id, computer_settled: confirmed });
  }

  observeRuntimeClient() {
    if (this.runtimeObservation) return this.runtimeObservation;
    const client = this.harness.client;
    const observation = this.runtimeObservation = { client, request: client.request, acknowledged: false };
    // Observe before start(): the official SDK disposes and replaces its client
    // when initialization fails. Proof must stay attached to the real child.
    client.request = async function (method, ...args) {
      const result = await observation.request.call(this, method, ...args);
      if (method === 'shutdown') observation.acknowledged = true;
      return result;
    };
    return observation;
  }

  async performClose() {
    if (!this.harness) return;
    const observation = this.observeRuntimeClient();
    const { client } = observation;
    // SDK close() owns the one shutdown RPC and can swallow its failure. Observe
    // that real response instead of sending a duplicate shutdown while the
    // Windows runtime is already disposing its native handles.
    let exited = false;
    try {
      await this.harness.close();
      exited = client.child?.exitCode === 0;
    } catch { /* Host may force its Job closed, but must not claim native input stopped. */ }
    finally { client.request = observation.request; }
    // Cordis logs some effect-disposal errors instead of propagating them.
    // A signed provider receipt therefore supplies the independent native ACK
    // evidence; a successful SDK shutdown alone is deliberately insufficient.
    const nativeSettled = this.request.options?.computer_enabled !== true || await verifyNativeReceipt(this.nativeReceipt);
    this.emitStopped(observation.acknowledged && exited && nativeSettled);
  }

  /** Serialize concurrent root/subagent questions into the host's one-question UI. */
  ask(frame, signal) {
    const work = async () => {
      if (this.cancelled || signal?.aborted) return null;
      const interaction_id = `interaction-${randomUUID().replaceAll('-', '')}`;
      return new Promise(resolve => {
        const finish = answer => { signal?.removeEventListener('abort', abort); resolve(answer); };
        const abort = () => { this.pending.delete(interaction_id); finish(null); };
        this.pending.set(interaction_id, finish);
        signal?.addEventListener('abort', abort, { once: true });
        this.emit({ type: 'interaction', id: this.request.id, interaction_id, kind: 'question', ...frame });
      });
    };
    const result = this.questionQueue.then(work, work);
    this.questionQueue = result.then(() => {}, () => {});
    return result;
  }

  async userQuestions(request, { signal }) {
    const answers = [];
    for (const question of request.questions) {
      const detail = [question.question, question.detail].filter(value => typeof value === 'string' && value).join('\n\n');
      // Never solicit approval of a plan whose details the host cannot display.
      if (!detail || textLength(detail) > 16000) return { cancelled: true };
      const options = (question.options ?? []).map(option => option.label);
      if (options.length > 6 || options.some(option => typeof option !== 'string' || textLength(option) > 240)) return { cancelled: true };
      const answer = await this.ask({ question: detail, options }, signal);
      if (!answer || answer.declined || answer.expired || typeof answer.text !== 'string' || !answer.text.trim()) return { cancelled: true };
      const text = answer.text.trim();
      if (question.intent?.kind === 'approval' && !options.includes(text)) return { cancelled: true };
      answers.push({ id: question.id, selected: options.includes(text) ? [text] : [],
        ...options.includes(text) ? {} : { custom: text } });
    }
    return { answers };
  }

  async run(frame) {
    // Host reserves a cleanup gate before submitting a run. Configuration,
    // helper lookup and filesystem errors can happen before any SDK exists;
    // acknowledge that no runtime was started even on these early exits.
    this.request = frame && typeof frame.id === 'string' ? { id: frame.id } : null;
    try { return await this.runWithRuntime(frame); }
    finally { if (!this.harness) this.emitStopped(true); }
  }

  async runWithRuntime(frame) {
    assertRuntimeSupported();
    this.request = validateRun(frame);
    const sessionId = frame.storage.session_id ?? `session-${randomUUID().replaceAll('-', '')}`;
    const launch = launchConfiguration(frame, this.computerOptions);
    const input = promptBlocks(frame);
    await mkdir(frame.storage.home, { recursive: true });
    await mkdir(launch.workspace, { recursive: true });
    if (frame.options.computer_enabled === true) this.nativeReceipt = await createNativeReceipt(frame.storage.home, sessionId, frame.id);
    // Boot dotenv discovery must not read the user's project .env. The SDK's
    // actual Session cwd and explicit sandbox workspace remain the selected folder.
    const processCwd = join(frame.storage.home, 'launch', randomUUID());
    await mkdir(processCwd, { recursive: true });
    const patchPath = join(frame.storage.home, 'jingban.launch.patch.json');
    const patchTemp = `${patchPath}.${randomUUID()}.tmp`;
    await writeFile(patchTemp, JSON.stringify(launch.patches, null, 2), { mode: 0o600 });
    await rename(patchTemp, patchPath);
    if (this.cancelled) throw failure('CANCELLED');
    const Harness = this.Harness ?? (await import('@deepseek-ai/dsh-sdk-client')).DeepSeekHarness;
    if (this.cancelled) throw failure('CANCELLED');
    this.harness = new Harness({ profile: 'sdk', dshHome: frame.storage.home, patches: [patchPath],
      cwd: launch.workspace, processCwd, provider: PROVIDER, model: frame.config.model,
      env: { ...runtimeEnvironment(frame.config), ...this.nativeReceipt ? {
        JINGBAN_DSH_CUA_RECEIPT_TOKEN: JSON.stringify(this.nativeReceipt),
      } : {} }, initializeTimeoutMs: 90000, shutdownTimeoutMs: 15000,
      disposeEofGraceMs: 10000, disposeGraceMs: 3000, approvalRequests: true,
      ...launch.reasoningEffort ? { reasoningEffort: launch.reasoningEffort } : {},
      onUserQuestion: this.userQuestions.bind(this),
    });
    const projection = new ActivityProjection(frame.id, this.emit);
    try {
      this.observeRuntimeClient();
      this.harness.client.start();
      this.harness.client.onRequest(async (method, params) => {
        if (method !== 'approval/request') throw failure('DSH_EXECUTION_FAILED');
        const tool = SAFE_TOOL.test(params.toolName ?? '') ? params.toolName : '工具';
        const reason = typeof params.reason === 'string' ? params.reason : '';
        const question = `允许本次 ${tool} 操作？${reason ? `\n${reason}` : ''}`;
        if (textLength(question) > 16000) return { outcome: 'rejected' };
        const answer = await this.ask({ question, options: ['允许本次', '拒绝'] });
        return { outcome: answer?.text === '允许本次' ? 'allowed-once' : answer ? 'rejected' : 'cancelled' };
      });
      this.emit({ type: 'progress', id: frame.id, phase: 'starting' });
      await this.harness.start();
      if (this.cancelled) throw failure('CANCELLED');
      if (frame.storage.session_id) {
        try { this.session = await this.harness.resumeSession(frame.storage.session_id); }
        catch { throw failure('DSH_SESSION_UNAVAILABLE'); }
      } else {
        this.session = this.harness.session(sessionId);
        // Create the identity without a model call before exposing it to the host.
        await this.session.getWorkingDirectory();
      }
      this.emit({ type: 'session', id: frame.id, dsh_session_id: this.session.id });
      const result = await this.session.run(input, { onNotification: notification => {
        projection.notification(notification);
        if (!this.canSteer && notification.method === 'session.event') this.canSteer = true;
        this.flushSteers().catch(() => { this.cancel().catch(() => {}); });
      } });
      this.canSteer = false;
      await this.close(); // Native persistence and owned child teardown precede delivery.
      if (this.cancelled) throw failure('CANCELLED');
      if (!this.shutdownConfirmed) throw failure('DSH_STOP_UNCONFIRMED');
      const reason = result.finishReason;
      if (reason === 'error' || reason == null) throw failure('DSH_MODEL_ERROR');
      if (reason === 'aborted' || reason === 'interrupted') throw failure('CANCELLED');
      const reply = typeof result.finalResponse === 'string' ? result.finalResponse : '';
      const completed = reason === 'completed';
      const output = { type: 'final', id: frame.id, reply: reply || (completed ? '已完成。' : '任务尚未完成。'),
        emotion: 'neutral', status: completed ? 'completed' : 'needs_user', backend: 'deepseek-harness',
        artifacts: await snapshotPresentedFiles(projection.artifacts.values()) };
      this.emit(output);
      return { ...output, tools: [...projection.tools], dsh_session_id: this.session.id };
    } finally {
      this.canSteer = false;
      for (const resolve of this.pending.values()) resolve(null);
      this.pending.clear();
      await this.close();
    }
  }
}
