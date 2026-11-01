// Dependency inspection only: imports modules, never calls screenshot or input APIs.
import { readFile } from 'node:fs/promises';
import { loadRuntime } from './sdk-adapter.mjs';
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
for (const method of ['log', 'info', 'warn', 'error', 'debug']) console[method] = () => {};
try {
  const runtime = await loadRuntime();
  const pkg = JSON.parse(await readFile(new URL('./package.json', import.meta.url), 'utf8'));
  write(JSON.stringify({ status: 'ready', platform: process.platform, node: process.version,
    sdk: pkg.dependencies['@ui-tars/sdk'], operator: pkg.dependencies['@ui-tars/operator-nut-js'],
    agentConstructor: typeof runtime.GUIAgent === 'function',
    operatorConstructor: typeof runtime.NutJSOperator === 'function',
    physicalInputExecuted: false, screenshotsCaptured: false, singleMonitor: true }) + '\n');
} catch (exception) {
  write(JSON.stringify({ status: 'unavailable', code: exception.code ?? 'SDK_UNAVAILABLE',
    physicalInputExecuted: false, screenshotsCaptured: false }) + '\n');
  process.exitCode = 1;
}
