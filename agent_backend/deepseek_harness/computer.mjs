/** Windows computer use supplied by the official DeepSeek Harness provider. */
import { existsSync } from 'node:fs';
import { dirname, isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const COMPUTER_PROVIDER = '@deepseek-ai/dsh-cua-windows';
export const COMPUTER_TOOL_NAMES = Object.freeze(['cua', 'cua_run_code']);

/** Locate the packaged, self-contained native helper; never build during a user task. */
export function computerHelperPath({ helperPath, architecture = process.arch, resolvePackage = import.meta.resolve } = {}) {
  if (helperPath !== undefined) {
    if (typeof helperPath !== 'string' || !isAbsolute(helperPath)) {
      throw new Error('COMPUTER_HELPER_PATH_INVALID');
    }
    return helperPath;
  }
  if (!['x64', 'arm64'].includes(architecture)) throw new Error('COMPUTER_ARCHITECTURE_UNSUPPORTED');
  const packageRoot = dirname(fileURLToPath(resolvePackage(`${COMPUTER_PROVIDER}/package.json`)));
  return join(packageRoot, 'native', 'bin', `win-${architecture}`, 'dsh-cua-windows.exe');
}

/** Read-only installation check. It never connects to the desktop or loads a model. */
export function inspectComputerRuntime(options = {}) {
  const platform = options.platform ?? process.platform;
  if (platform !== 'win32') return { available: false, code: 'COMPUTER_PLATFORM_UNSUPPORTED' };
  try {
    const helperPath = computerHelperPath(options);
    const available = (options.fileExists ?? existsSync)(helperPath);
    return { available, code: available ? 'READY' : 'COMPUTER_HELPER_MISSING', helperPath,
      provider: COMPUTER_PROVIDER, tool: 'cua' };
  } catch (error) {
    const known = ['COMPUTER_HELPER_PATH_INVALID', 'COMPUTER_ARCHITECTURE_UNSUPPORTED'];
    return { available: false, code: known.includes(error?.message) ? error.message : 'COMPUTER_PROVIDER_MISSING' };
  }
}

/**
 * Loader patch rows for the SDK profile. Disabling computer access mounts no provider.
 * The provider owns its official tool schemas, screenshots, input, application consent,
 * cancellation and native transport. No legacy planner or GUI model is started.
 */
export function computerPatch(options = {}) {
  if (options.enabled !== true) return [];
  const status = inspectComputerRuntime(options);
  if (!status.available) throw new Error(status.code);
  // SDK profiles live under a private per-run home. Resolve from our locked
  // installation before handing the profile to its separate package loader.
  const resolvePackage = options.resolvePackage ?? import.meta.resolve;
  return [{ insert: [
    { id: 'jingban-computer-use', name: resolvePackage('@deepseek-ai/dsh-computer-use') },
    { id: 'jingban-cua-windows', name: resolvePackage(COMPUTER_PROVIDER), config: {
      helperPath: status.helperPath,
      // The published 0.2.2-rc.3 core has no presets in its SDK profile and does
      // not include master's later cua_run_code registration. Publish raw cua
      // globally so the SDK Agent receives the official computer tool.
      agentPreset: false,
    } },
  ] }];
}
