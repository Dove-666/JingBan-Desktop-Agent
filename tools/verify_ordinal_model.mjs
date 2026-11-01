/** Read-only real-provider verification on OWN fixture images, never desktop input. */
import { verifyVisualGoal } from '../agent_backend/ui_tars/goal-verifier.mjs';
import { modelDefaultFetch, isModernGptModel } from '../agent_backend/ui_tars/sdk-adapter.mjs';

let size = 0; const chunks = [];
for await (const chunk of process.stdin) {
  size += chunk.length;
  if (size > 32 * 1024 * 1024) throw new Error('FIXTURE_INPUT_TOO_LARGE');
  chunks.push(chunk);
}
const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const results = [];
try {
  if (!request.config || !Array.isArray(request.cases) || request.cases.length > 4) throw new Error('FIXTURE_INVALID');
  for (const c of request.cases) {
    const started = Date.now();
    const result = await verifyVisualGoal({ task: c.task, observations: c.observations,
      config: request.config, signal: new AbortController().signal,
      fetchImpl: modelDefaultFetch(globalThis.fetch, isModernGptModel(request.config.model)) });
    results.push({ name: c.name, verification: result, seconds: (Date.now() - started) / 1000 });
  }
  process.stdout.write(JSON.stringify({ results }));
} finally {
  request.config.apiKey = '';
}
