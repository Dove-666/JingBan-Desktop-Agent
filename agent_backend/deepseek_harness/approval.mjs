/** Permission choices owned by the host; model text never chooses this value. */
export const APPROVAL_MODES = Object.freeze(['ask', 'auto', 'full']);

export function approvalMode(value = 'auto') {
  if (!APPROVAL_MODES.includes(value)) throw new Error('INVALID_CONFIG');
  return value;
}

export function permissionPreset(mode) {
  return { ask: 'workspace-write', auto: 'auto', full: 'danger-full-access' }[approvalMode(mode)];
}

/** Only native CUA access grants are implicit; task clarification stays interactive. */
export function automaticComputerAnswer(question, mode) {
  if (!['auto', 'full'].includes(mode) || !['cua-app-access', 'cua-mouse-control'].includes(question.id)
      || question.intent?.kind !== 'approval') return null;
  const approve = question.intent.approve;
  if (typeof approve !== 'string' || !question.options?.some(option => option.label === approve)) return null;
  return { id: question.id, selected: [approve] };
}

export function approvalOutcome(answer, cancelled = false) {
  if (cancelled || !answer || answer.expired || answer.declined) return 'cancelled';
  return answer.text === '允许本次' ? 'allowed-once' : 'rejected';
}

export function toolArgumentsDetail(argumentsValue) {
  let value = argumentsValue;
  // Upstream Session tool/call events carry serialized arguments; the approval
  // card should show the object once, not a doubly escaped JSON string.
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return value; }
  }
  return JSON.stringify(value, null, 2);
}
