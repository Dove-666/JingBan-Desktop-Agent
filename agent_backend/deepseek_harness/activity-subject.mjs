/** Small public subjects derived from official tool schemas, never their payloads. */
export const COMPUTER_ACTIONS = Object.freeze([
  'capabilities', 'list_apps', 'find_apps', 'open_app', 'activate', 'windows', 'inspect',
  'inspect_controls', 'interact', 'click', 'double_click', 'right_click', 'move', 'input',
  'select_text', 'paste', 'keypress', 'scroll', 'scroll_up', 'scroll_down', 'scroll_left',
  'scroll_right', 'drag', 'screenshot',
]);

const COMPUTER_METHODS = Object.freeze({
  getCapabilities: 'capabilities', listApps: 'list_apps', findApps: 'find_apps', getApp: 'open_app',
  activate: 'activate', show: 'activate', listWindows: 'windows', getState: 'inspect',
  listActions: 'inspect_controls', performAction: 'interact', invoke: 'click', mouseMove: 'move',
  click: 'click', setValue: 'input', selectText: 'select_text', typeText: 'input', paste: 'paste',
  pressKey: 'keypress', sendKeyEvent: 'keypress', scroll: 'scroll', drag: 'drag', screenshot: 'screenshot',
});
const FILE_TOOLS = new Set(['read', 'write', 'edit', 'read_image', 'glob', 'grep', 'ls']);
const SECRET = /(?:\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{12,}|\bBearer\s+\S+|(?:api[_ -]?key|password|passwd|secret|access[_ -]?token)\s*[:=])/i;

export function parsedArguments(value) {
  if (typeof value === 'string') {
    if (value.length > 2 * 1024 * 1024) return null;
    try { value = JSON.parse(value); } catch { return null; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

export function safePublicName(value) {
  if (typeof value !== 'string') return null;
  const name = value.trim();
  if (!name || name.length > 96 || name === '.' || name === '..'
      || /[\x00-\x1f\x7f/\\:?<>"`@=;{}|\p{Cf}]/u.test(name) || SECRET.test(name)) return null;
  return name;
}

export function fileName(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\x00-\x1f\x7f]/u.test(value)) return null;
  return safePublicName(value.replace(/[\\/]+$/u, '').split(/[\\/]/u).at(-1));
}

function website(value) {
  if (typeof value !== 'string' || value.length > 8192) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    return /^[a-z0-9.-]{1,96}$/i.test(url.hostname) ? url.hostname : null;
  } catch { return null; }
}

function fileSubject(paths) {
  if (!Array.isArray(paths) || !paths.length || paths.length > 1000) return null;
  const names = paths.map(fileName);
  if (names.some(name => !name)) return null;
  return names.length === 1 ? { kind: 'file', name: names[0] } : { kind: 'files', count: names.length };
}

export function computerSubject(args, appName) {
  if (!args || !Object.hasOwn(COMPUTER_METHODS, args.method)) return null;
  let action = COMPUTER_METHODS[args.method];
  const params = parsedArguments(args.params) ?? {};
  if (args.method === 'click') {
    action = params.mouseButton === 'right' ? 'right_click' : params.clickCount === 2 ? 'double_click' : 'click';
  } else if (args.method === 'scroll') {
    const x = params.deltaLinesX, y = params.deltaLinesY;
    if (Number.isFinite(y) && y !== 0) action = y > 0 ? 'scroll_down' : 'scroll_up';
    else if (Number.isFinite(x) && x !== 0) action = x > 0 ? 'scroll_right' : 'scroll_left';
  }
  // Only an executable basename is a usable pre-result getApp selector; AUMIDs
  // are opaque implementation identities, not display names.
  const name = safePublicName(appName) ?? (args.method === 'getApp' && /\.exe$/i.test(params.selector ?? '')
    ? fileName(params.selector) : null);
  return { kind: 'computer', action, ...name ? { name } : {} };
}

export function operationSubject(name, argumentsValue, appName) {
  const args = parsedArguments(argumentsValue);
  if (!args) return null;
  if (name === 'cua') return computerSubject(args, appName);
  if (FILE_TOOLS.has(name)) return fileSubject([args.file_path ?? args.path]);
  if (name === 'present') return fileSubject(Array.isArray(args.files) ? args.files.map(file => file?.path) : null);
  if (name === 'apply_patch' && typeof args.input === 'string') {
    const paths = [...args.input.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)\r?$/gm)]
      .map(match => match[1].trim());
    return fileSubject([...new Set(paths)]);
  }
  if (name === 'web_fetch') {
    const host = website(args.url);
    return host ? { kind: 'website', name: host } : null;
  }
  if (['pwsh', 'bash'].includes(name)) {
    const description = safePublicName(args.description);
    return description ? { kind: 'command', name: description } : null;
  }
  if (['subagent', 'subagent_fork'].includes(name)) {
    const label = safePublicName(args.name) ?? safePublicName(args.description);
    return { kind: 'agent', action: 'start', ...label ? { name: label } : {} };
  }
  if (['todo_write', 'update_plan'].includes(name)) {
    const items = args.todos ?? args.steps;
    if (Array.isArray(items) && items.length <= 1000) return { kind: 'plan', count: items.length };
  }
  return null;
}

/** Public assistant TextBlock only. This is not a summarizer for hidden reasoning. */
export function publicCommentary(value) {
  if (typeof value !== 'string') return '';
  let text = value.slice(0, 16000)
    .replace(/<(think|thinking|reasoning|analysis)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gi, '')
    .replace(/```[\s\S]*?(?:```|$)/g, '[代码已省略]')
    .replace(/`[^`\n]*(?:`|$)/g, '[代码已省略]')
    .replace(/data:[^\s<>"']+/gi, '[图片已省略]')
    .replace(/https?:\/\/[^\s<>"'，。；）)]+/gi, value => website(value) ?? '[网址已省略]')
    .replace(/\bBearer\s+\S+/gi, '[凭据已省略]')
    .replace(/\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{12,}/gi, '[凭据已省略]')
    .replace(/["']?\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token)["']?\s*[:=]\s*(?:"[^"\n]*(?:"|(?=\n)|$)|'[^'\n]*(?:'|(?=\n)|$)|[^\s，。；]+)/gi, '[凭据已省略]')
    .replace(/[A-Za-z]:[\\/][^\n"'<>，。；]+/g, '[文件路径已省略]')
    .replace(/\\\\[^\n"'<>，。；]+/g, '[文件路径已省略]')
    .replace(/(^|[\s(（])\/(?:[^\s/]+\/)+[^\s，。；)）]*/g, '$1[文件路径已省略]')
    .replace(/[\x00-\x08\x0b-\x1f\x7f\p{Cf}]/gu, '')
    .replace(/\n{3,}/g, '\n\n').trim();
  if (text.length > 3000) text = text.slice(0, 2999) + '…';
  return text;
}

/** The sole parsed result is the official getApp identity; screenshots/results stay private. */
export function selectedApplication(message) {
  if (message?.isError || !Array.isArray(message?.content)) return null;
  const blocks = message.content.filter(block => block?.type === 'text' && typeof block.text === 'string');
  if (blocks.length !== 1 || blocks[0].text.length > 65536) return null;
  const value = parsedArguments(blocks[0].text);
  const name = safePublicName(value?.name);
  return name && typeof value?.appHandle === 'string' && value.appHandle.length <= 512
    && typeof value.running === 'boolean' ? { name, handle: value.appHandle } : null;
}
