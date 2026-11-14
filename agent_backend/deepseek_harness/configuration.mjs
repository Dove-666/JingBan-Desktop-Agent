/** Private launch configuration for the official DeepSeek Harness SDK profile. */
import { isAbsolute, resolve } from 'node:path';
import { computerPatch } from './computer.mjs';

export const PROVIDER = 'jingban-shared';
export const KEY_VARIABLE = 'JINGBAN_DSH_API_KEY';
const LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
// Match Python len()/Android codePointCount at the private protocol boundary.
export const textLength = text => Array.from(text).length;
const truncateText = (text, limit) => Array.from(text).slice(0, limit).join('');

export function reasoningOptions(value = {}) {
  const profile = value.profile ?? 'auto';
  const selected = value.value ?? 'default';
  if (profile === 'auto' || profile === 'omit' || value.control === 'none') return {};
  const compat = {};
  if (profile === 'deepseek' || profile === 'thinking') {
    compat.thinkingFormat = 'deepseek';
    if (profile === 'deepseek') Object.assign(compat, {
      // Preserve the provider contract through gateways and local test proxies.
      supportsDeveloperRole: false, supportsStore: false, maxTokensField: 'max_tokens',
      requiresReasoningContentOnAssistantMessages: true,
    });
    else compat.supportsReasoningEffort = false;
  } else if (profile === 'enable_thinking' || profile === 'qwen_budget') {
    compat.thinkingFormat = 'qwen';
    compat.supportsReasoningEffort = false;
  } else if (profile === 'openrouter') compat.thinkingFormat = 'openrouter';
  else if (['openai', 'legacy', 'custom', 'gemini'].includes(profile)) compat.thinkingFormat = 'openai';
  else return {}; // Unmapped gateway-specific extensions stay provider defaults.
  const reasoningEfforts = {};
  for (const wire of value.values ?? []) {
    const level = wire === 'none' ? 'off' : wire === 'enabled' ? 'high' : wire;
    if (LEVELS.has(level)) reasoningEfforts[level] = level === 'off' ? (selected === 'none' ? 'none' : null) : wire === 'enabled' ? 'high' : wire;
  }
  let effort = selected === 'none' ? 'off' : selected === 'enabled' ? 'high' : selected;
  if (selected !== 'default' && !LEVELS.has(effort) && profile === 'custom') {
    effort = 'max';
    reasoningEfforts.max = selected;
  }
  if (!Object.keys(reasoningEfforts).length) return {};
  const result = { model: { reasoningEfforts, compat } };
  if (Object.hasOwn(reasoningEfforts, effort)) result.effort = effort;
  if (profile === 'qwen_budget' && Number.isSafeInteger(value.budget_tokens) && value.budget_tokens > 0) {
    compat.supportsThinkingTokenBudget = true;
    compat.thinkingTokenBudgetField = 'thinking_budget';
    result.thinkingBudgets = { minimal: value.budget_tokens, low: value.budget_tokens,
      medium: value.budget_tokens, high: value.budget_tokens };
  }
  return result;
}

/** The env is rebuilt so unrelated account credentials never enter this runtime. */
export function runtimeEnvironment(config, parent = process.env) {
  const env = {};
  const allowed = /^(PATH|PATHEXT|SYSTEMROOT|WINDIR|COMSPEC|TEMP|TMP|USERPROFILE|HOMEDRIVE|HOMEPATH|APPDATA|LOCALAPPDATA|PROGRAMFILES|PROGRAMFILES\(X86\)|PROGRAMDATA|NUMBER_OF_PROCESSORS|PROCESSOR_ARCHITECTURE|LANG|LC_ALL|TZ|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|SSL_CERT_FILE|NODE_EXTRA_CA_CERTS)$/i;
  for (const [name, value] of Object.entries(parent)) if (allowed.test(name) && typeof value === 'string') env[name] = value;
  env[KEY_VARIABLE] = config.api_key;
  env.DSH_TELEMETRY_DISABLED = '1';
  env.DSH_PRIMARY_RUNTIME = '';
  env.DSH_MAX_TOKENS_AS_SUCCESS = 'false';
  return env;
}

export function validateRun(frame) {
  if (!frame || frame.type !== 'run' || typeof frame.id !== 'string' || !frame.id ||
      typeof frame.task !== 'string' || !frame.task.trim() || textLength(frame.task) > 12000) throw new Error('INVALID_REQUEST');
  if (!frame.config || typeof frame.config.api_key !== 'string' || !frame.config.api_key ||
      typeof frame.config.model !== 'string' || !frame.config.model.trim()) throw new Error('INVALID_CONFIG');
  const endpoint = new URL(frame.config.base_url);
  if (!['https:', 'http:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error('INVALID_CONFIG');
  if (!frame.storage || !isAbsolute(frame.storage.home) || !frame.options || !isAbsolute(frame.options.workspace)) throw new Error('INVALID_CONFIG');
  if (frame.storage.session_id != null && !/^session-[a-zA-Z0-9-]{1,150}$/.test(frame.storage.session_id)) throw new Error('INVALID_SESSION');
  return frame;
}

/** JSON is valid YAML; only non-secret configuration goes into this launch patch. */
export function launchConfiguration(frame, computerOptions = {}) {
  validateRun(frame);
  const config = frame.config;
  const reasoning = reasoningOptions(config.reasoning);
  const contextWindow = Number.isSafeInteger(config.context_window) && config.context_window >= 8192 ? config.context_window : 131072;
  const model = { id: config.model, contextWindow, maxTokens: 32768,
    input: config.supports_vision === false ? ['text'] : ['text', 'image'], ...reasoning.model };
  const profile = { apiKeyEnv: KEY_VARIABLE, api: config.protocol ?? 'openai-completions',
    baseURL: config.base_url.replace(/\/$/, ''), models: [model],
    retryPolicy: { mode: 'normal', maxRetries: 2 }, timeoutMs: 300000, streamIdleTimeoutMs: 300000,
    ...reasoning.thinkingBudgets ? { thinkingBudgets: reasoning.thinkingBudgets } : {} };
  const persona = typeof frame.context?.personality === 'string' ? truncateText(frame.context.personality,16000) : '';
  const deepseekSearch = new URL(config.base_url).origin === 'https://api.deepseek.com';
  const patches = [
    { id: 'session-telemetry-otel', disabled: true },
    { id: 'credentials', disabled: true },
    { id: 'authorization', disabled: true },
    { id: 'deepseek-account', disabled: true },
    { id: 'connection', disabled: true }, // No DSH Web server/browser-auth plane in this embedding.
    { id: 'file-upload', disabled: true }, // SDK inline images use attachments directly.
    { id: 'llm-deepseek', disabled: true },
    { id: 'llm-deepseek-account', disabled: true },
    { id: 'llm-pi-ai', config: { providers: { [PROVIDER]: profile } } },
    { id: 'agent-default-model', config: { provider: PROVIDER, model: config.model } },
    { id: 'sandbox-policy', config: { mode: 'workspace-write', workspaceRoot: resolve(frame.options.workspace) } },
    { id: 'system-prompt', config: { personaPrefix: `你是鲸伴桌面智能体。直接使用可用工具完成用户任务，依据实际工具结果汇报；最终回复使用用户的语言。交付文件时使用 present 工具。\n${persona}` } },
    { id: 'tool-plugin-manager', disabled: true },
    { id: 'tool-schedule', disabled: true }, // A per-turn process cannot deliver future schedules.
    { id: 'web-search-deepseek', disabled: true },
    { id: 'tool-web', config: { search: deepseekSearch, fetch: true, searchTimeoutMs: 60000 } },
  ];
  if (deepseekSearch) patches.push({ id: 'web-deepseek-api', config: {
    // An explicit key reference keeps the official plugin's /v1/tools base.
    searchApiKeyEnv: KEY_VARIABLE,
  } });
  if (frame.options.tools_enabled === false) {
    for (const id of ['tool-fs', 'tool-fs-search', 'tool-bash', 'tool-pwsh', 'tool-web', 'tool-jobs',
      'tool-workflow', 'tool-subagent', 'tool-subagent-fork', 'tool-subagent-control', 'tool-agent-team',
      'tool-office', 'workspace-dependencies', 'tool-skill', 'tool-goal']) patches.push({ id, disabled: true });
  }
  patches.push(...computerPatch({ ...computerOptions, enabled: frame.options.computer_enabled === true }));
  return { patches, reasoningEffort: reasoning.effort, workspace: resolve(frame.options.workspace) };
}

function plainText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter(block => block && block.type === 'text' && typeof block.text === 'string').map(block => block.text).join('\n');
}

/** Ordinary history is supplied once; only this turn's explicit image is uploaded. */
export function promptBlocks(frame) {
  const blocks = [];
  if (!frame.storage.session_id && Array.isArray(frame.context?.messages)) {
    const history = frame.context.messages.filter(message => ['user', 'assistant'].includes(message?.role))
      .slice(-24).map(message => ({ role: message.role, content: truncateText(plainText(message.content),12000) }))
      .filter(message => message.content);
    if (history.at(-1)?.role === 'user' && history.at(-1).content.trim() === frame.task.trim()) history.pop();
    if (history.length) blocks.push({ type: 'text', text: `此前对话记录，仅供理解上下文，已完成的操作不要重复执行：\n${JSON.stringify(history)}\n当前请求如下：` });
  }
  blocks.push({ type: 'text', text: frame.task });
  const image = frame.context?.current_image_url;
  if (image) {
    if (typeof image !== 'string' || image.length > 16 * 1024 * 1024) throw new Error('INVALID_IMAGE');
    const match = /^data:(image\/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})$/.exec(image);
    if (!match || Buffer.from(match[2], 'base64').toString('base64') !== match[2]) throw new Error('INVALID_IMAGE');
    blocks.push({ type: 'image', mimeType: match[1], data: match[2] });
  }
  return blocks;
}
