/** Official fixed branches: Node 24.20 and 26.7 (nodejs/node#61999). */
export function runtimeSupported(version = process.versions.node) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version ?? '');
  if (!match) return false;
  const major = Number(match[1]), minor = Number(match[2]);
  return (major === 24 && minor >= 20) || (major === 26 && minor >= 7) || major > 26;
}

export function assertRuntimeSupported() {
  if (runtimeSupported()) return;
  const error = new Error('DSH_RUNTIME_UNSUPPORTED');
  error.code = 'DSH_RUNTIME_UNSUPPORTED';
  throw error;
}
