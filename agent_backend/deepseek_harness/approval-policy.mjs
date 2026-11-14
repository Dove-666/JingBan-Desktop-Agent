/** Host ask-mode gate over the official tool pipeline, not an OS security boundary. */
import { isAbsolute, relative } from 'node:path';

export const name = 'jingban-approval-policy';
export const inject = ['tools', 'fs', 'workingDirectory'];

// Only tools with known local/read-only or orchestration effects are exempt.
// New or arbitrary-code tools are deliberately not classified by command keywords.
const ROUTINE = new Set(['read', 'read_image', 'glob', 'grep', 'ls', 'present',
  'ask_user_question', 'job_list', 'job_output', 'job_kill', 'subagent', 'subagent_fork',
  'subagent_wait', 'subagent_send', 'subagent_stop', 'update_plan', 'get_goal', 'create_goal', 'update_goal']);

export function contained(path, root) {
  const tail = relative(root, path);
  return tail === '' || (!isAbsolute(tail) && tail !== '..' && !tail.startsWith('..\\') && !tail.startsWith('../'));
}

export function mutationPaths(exec) {
  if (['write', 'edit'].includes(exec.name)) {
    return typeof exec.arguments?.file_path === 'string' ? [exec.arguments.file_path] : null;
  }
  if (exec.name !== 'apply_patch') return null;
  if (typeof exec.arguments?.input !== 'string') return null;
  const paths = [...exec.arguments.input.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)\r?$/gm)]
    .map(match => match[1].trim());
  return paths.length ? paths : null;
}

export async function askDecision(ctx, exec, workspace) {
  if (exec.signal.aborted) return { kind: 'cancel' };
  if (ROUTINE.has(exec.name)) return { kind: 'allow' };
  const paths = mutationPaths(exec);
  if (paths) {
    // Resolve through the exact same provider as the file tool, including symlinks.
    const cwd = exec.agent ? ctx.workingDirectory.get(exec.agent.session) : workspace;
    const root = await ctx.fs.resolve(workspace, { signal: exec.signal });
    const targets = await Promise.all(paths.map(path => ctx.fs.resolve(path, { cwd, signal: exec.signal })));
    if (targets.every(target => contained(target.targetKey, root.targetKey))) return { kind: 'allow' };
    // The upstream file tool owns single-call escalation and its audited approval.
    // Require it even for temp folders, which upstream workspace-write otherwise allows.
    if (exec.arguments.sandbox_permissions === 'danger-full-access'
        && typeof exec.arguments.justification === 'string' && exec.arguments.justification.trim()) return { kind: 'allow' };
    return { kind: 'deny', reason: 'This operation changes files outside the selected workspace. '
      + 'Retry this exact file operation with sandbox_permissions="danger-full-access" and a justification '
      + 'naming the paths and changes; the user must approve before any mutation.' };
  }
  const detail = JSON.stringify(exec.arguments);
  if (typeof detail !== 'string' || Array.from(detail).length > 12000) {
    return { kind: 'deny', reason: 'The proposed operation is too large to display for approval. Split it into smaller operations.' };
  }
  return { kind: 'ask', reason: '此操作可能联网或修改文件，需要你批准。' };
}

export function apply(ctx, config) {
  if (config.mode !== 'ask') throw new Error('Only ask mode mounts the host approval gate');
  if (typeof config.workspace !== 'string' || !isAbsolute(config.workspace)) throw new Error('Missing approval workspace');
  ctx.on('tools/pre-execute', async (exec, next) => {
    const downstream = await next();
    if (downstream.kind !== 'allow') return downstream;
    return askDecision(ctx, exec, config.workspace);
  });
}
