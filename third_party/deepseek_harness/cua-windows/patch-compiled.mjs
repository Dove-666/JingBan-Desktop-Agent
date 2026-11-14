/** Match the newer official Windows provider to the published shared CUA API. */
import { readFileSync, writeFileSync } from 'node:fs';
const entry = new URL('./lib/index.js', import.meta.url);
let compiled = readFileSync(entry, 'utf8');
const marker = 'getGuidance: () => GUIDANCE,';
const compatible = 'guidance: GUIDANCE, getGuidance: () => GUIDANCE,';
if (!compiled.includes(compatible) && compiled.split(marker).length !== 2) {
  throw new Error('The upstream Windows guidance registration changed; review its published-core compatibility.');
}
if (!compiled.includes(compatible)) compiled = compiled.replace(marker, compatible);
const channel = 'createChannel: (_owner, native) => runtime.connect(native),';
const bridged = 'createChannel: (_owner, native) => publishedCuaChannel(runtime.connect(native)),';
if (!compiled.includes(bridged)) {
  if (compiled.split(channel).length !== 2) throw new Error('Review upstream CUA channel registration compatibility.');
  compiled = "import { publishedCuaChannel } from '../compat-channel.mjs';\n" + compiled.replace(channel, bridged);
}
const constructor = 'const runtime = new WindowsNativeRuntime(ctx, config.pollIntervalMs, launchWaitMs, host === "wsl" ? "stdio" : "pipe");';
const tracked = 'const runtime = installNativeReceipt(new WindowsNativeRuntime(ctx, config.pollIntervalMs, launchWaitMs, host === "wsl" ? "stdio" : "pipe"));';
if (!compiled.includes(tracked)) {
  if (compiled.split(constructor).length !== 2) throw new Error('Review upstream native lifetime registration compatibility.');
  compiled = compiled.replace("import { publishedCuaChannel } from '../compat-channel.mjs';",
    "import { publishedCuaChannel, installNativeReceipt } from '../compat-channel.mjs';").replace(constructor, tracked);
}
writeFileSync(entry, compiled);
