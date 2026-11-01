/** Synthetic-only compatibility probe. It never imports a native operator. */
import { pathToFileURL } from 'node:url';
import { validateRun } from './bridge.mjs';
import { createSdkAgent, isModernGptModel } from './sdk-adapter.mjs';
import { verificationReport } from './goal-verifier.mjs';

export const SYNTHETIC_LAYOUT = Object.freeze({ width: 840, height: 560,
  target: { left: 504, top: 252, right: 756, bottom: 364 } });

export async function runSyntheticProbe(input, { signal: externalSignal } = {}) {
  const run = validateRun({ type: 'run', id: 'synthetic-probe', task: 'synthetic', config: input?.config,
    options: { maxSteps: 2, timeoutMs: 65000 } });
  if (run.config.provider !== 'ui-tars-1.5') throw new Error('PROBE_PROVIDER_REQUIRED');
  const controller = new AbortController();
  const cancel = () => controller.abort();
  externalSignal?.addEventListener('abort', cancel, { once: true });
  if (externalSignal?.aborted) cancel();
  const timer = setTimeout(cancel, 65000);
  let screenshots = 0; let clicks = 0; let clicked = false; let finished = false; let failure = null;
  let lastPoint = null; let handle;
  try {
    const [{ GUIAgent }, { parseBoxToScreenCoords }, { Jimp, loadFont }, fonts] = await Promise.all([
      import('@ui-tars/sdk'), import('@ui-tars/sdk/core'), import('jimp'), import('jimp/fonts'),
    ]);
    const font = await loadFont(fonts.SANS_32_BLACK);
    const layout = SYNTHETIC_LAYOUT;
    const frame = async (done) => {
      const image = new Jimp({ width: layout.width, height: layout.height, color: 0xf0f4f8ff });
      image.composite(new Jimp({ width: 840, height: 56, color: 0xdbe4edff }), 0, 0);
      image.print({ font, x: 28, y: 8, text: 'AURA SYNTHETIC TEST' });
      image.print({ font, x: 56, y: 112, text: done ? 'DONE' : 'READY' });
      image.composite(new Jimp({ width: 252, height: 112, color: 0xee8e8eff }), 84, 252);
      image.composite(new Jimp({ width: 252, height: 112, color: 0x57d386ff }), 504, 252);
      image.print({ font, x: 122, y: 290, text: 'CANCEL' });
      image.print({ font, x: 546, y: 290, text: 'TARGET' });
      return (await image.getBuffer('image/png')).toString('base64');
    };
    const frames = { ready: await frame(false), done: await frame(true) };
    class FakeOperator {
      static MANUAL = { ACTION_SPACES: ["click(start_box='(x,y)') # Absolute screenshot pixel coordinates.", 'finished()'] };
      async screenshot() { screenshots++; return { base64: clicked ? frames.done : frames.ready, scaleFactor: 1 }; }
      async execute(params) {
        if (params.parsedPrediction.action_type !== 'click') { failure = 'UNEXPECTED_ACTION'; throw new Error(failure); }
        const point = parseBoxToScreenCoords({ boxStr: params.parsedPrediction.action_inputs?.start_box,
          screenWidth: params.screenWidth, screenHeight: params.screenHeight });
        lastPoint = point;
        const { left, top, right, bottom } = layout.target;
        if (clicked || !Number.isFinite(point.x) || !Number.isFinite(point.y) ||
          point.x < left || point.x > right || point.y < top || point.y > bottom) {
          failure = 'TARGET_MISSED'; throw new Error(failure);
        }
        clicked = true; clicks++;
      }
    }
    handle = await createSdkAgent({ ...run, signal: controller.signal,
      onEvent(event) { if (event.action === 'finished' && event.status === 'completed') finished = true; } }, { GUIAgent, NutJSOperator: FakeOperator,
      // This probe owns an exact, deterministic synthetic postcondition. It
      // never claims to validate a real desktop or use a judge model.
      async verifyGoal({ observations }) {
        return clicked && clicks === 1 && observations.at(-1)?.base64 === frames.done ?
          verificationReport('verified', 'GOAL_STATE_CONFIRMED') : verificationReport('failed', 'GOAL_STATE_NOT_MET');
      } });
    const result = await handle.run(`This is a synthetic image, not a real computer. It is exactly 840 pixels wide and 560 pixels high.\n` +
      `Use absolute image pixel coordinates, not normalized coordinates. First click the GREEN button labeled TARGET exactly once.\n` +
      `On the next screenshot, when READY has changed to DONE, output finished(). Do not click CANCEL or click twice.\n` +
      `For each response use exactly Thought: <brief action> followed by Action: click(start_box='(x,y)') or Action: finished().`);
    const passed = result.status === 'completed' && clicked && clicks === 1 && finished && screenshots === 3;
    return { status: passed ? 'passed' : 'failed', code: passed ? null : failure ?? 'SEQUENCE_INCOMPLETE',
      check: 'synthetic_format_and_initial_grounding_only', steps: screenshots - 1, verificationObservations: 1, clicks,
      parsedTargetHit: clicked, finished, ...(lastPoint && { parsedPoint: lastPoint }),
      requestProfile: isModernGptModel(run.config.model) ? 'modern-gpt' : 'ui-tars-default',
      modelRequestsMaximum: 2, desktopScreenshots: 0, physicalInputActions: 0 };
  } catch (exception) {
    return { status: 'failed', code: controller.signal.aborted ? 'PROBE_CANCELLED_OR_TIMED_OUT' :
      failure ?? (['MODEL_FAILED', 'STEP_LIMIT', 'ACTION_FAILED', 'SDK_FAILED'].includes(exception?.code) ? exception.code : 'PROBE_FAILED'),
      check: 'synthetic_format_and_initial_grounding_only', steps: screenshots, clicks,
      parsedTargetHit: clicked, finished, ...(lastPoint && { parsedPoint: lastPoint }),
      modelRequestsMaximum: 2, desktopScreenshots: 0, physicalInputActions: 0 };
  } finally {
    clearTimeout(timer); externalSignal?.removeEventListener('abort', cancel);
    run.config.apiKey = ''; handle = null;
  }
}

async function main() {
  const write = process.stdout.write.bind(process.stdout);
  process.stdout.write = () => true;
  process.stderr.write = () => true;
  for (const method of ['log', 'info', 'warn', 'error', 'debug', 'trace']) console[method] = () => {};
  let length = 0; const chunks = [];
  try {
    for await (const chunk of process.stdin) {
      length += chunk.length;
      if (length > 128 * 1024) throw new Error('INVALID_INPUT');
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const result = await runSyntheticProbe(input);
    write(JSON.stringify(result) + '\n');
    process.exitCode = result.status === 'passed' ? 0 : 1;
  } catch {
    write(JSON.stringify({ status: 'failed', code: 'INVALID_INPUT', desktopScreenshots: 0, physicalInputActions: 0 }) + '\n');
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
