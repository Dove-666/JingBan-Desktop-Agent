import test from 'node:test';
import assert from 'node:assert/strict';
import { BoundedObservations } from '../bounded-observations.mjs';

const clock = () => '2026-11-01T15:00:00.000Z';

test('short app/feed/first/second/like run retains every actual frame in order', () => {
  const history = new BoundedObservations({ clock });
  const frames = ['desktop', 'app-menu', 'home-feed', 'first-video', 'second-video', 'second-liked', 'fresh-final'];
  for (const [index, frame] of frames.entries()) {
    history.capture(frame);
    if (index < frames.length - 2) history.actionSucceeded('click');
  }
  assert.deepEqual(history.observations().map(f => f.base64), frames);
  assert.deepEqual(history.observations().map(f => f.sequence), [1, 2, 3, 4, 5, 6, 7]);
  assert.equal(history.latest.base64, 'fresh-final');
  assert.equal(history.latest.after_action, null);
  assert.ok(history.observations().every(f => f.captured_at === clock()));
});

test('over one hundred observations retain first and recent navigation pairs within eight images', () => {
  const history = new BoundedObservations({ clock });
  for (let sequence = 1; sequence <= 120; sequence++) {
    history.capture('actual-frame-' + sequence);
    if ([20, 98].includes(sequence)) history.actionSucceeded('scroll');
    else if (sequence < 120) history.actionSucceeded('click');
    assert.ok(history.size <= 8);
    assert.ok(history.bytes <= history.maxBytes);
    assert.ok(history.frames.length <= 8);
  }
  const frames = history.observations();
  const indices = frames.map(f => f.sequence);
  for (const required of [1, 2, 3, 20, 21, 98, 99, 120]) assert.ok(indices.includes(required), String(required));
  assert.deepEqual(indices, [...indices].sort((a, b) => a - b));
  assert.equal(new Set(indices).size, indices.length);
  assert.equal(frames.find(f => f.sequence === 21).after_action, 'scroll');
  assert.equal(frames.find(f => f.sequence === 99).after_action, 'scroll');
  assert.equal(frames.at(-1).base64, 'actual-frame-120');
  assert.ok(indices.some((n, i) => i && n - indices[i - 1] > 1), 'sampling gaps remain explicit');
  assert.equal(history.summary().omitted_observations, 112);
});

test('identical captured pixels keep distinct observation identity and actual action transition', () => {
  const history = new BoundedObservations({ clock });
  history.capture('same-real-pixels');
  history.actionSucceeded('scroll');
  history.capture('same-real-pixels');
  history.capture('same-real-pixels');
  assert.deepEqual(history.observations().map(f => f.sequence), [1, 2, 3]);
  assert.equal(history.observations()[1].after_action, 'scroll');
  assert.equal(history.observations()[2].after_action, null);
});

test('metadata cannot contain arbitrary action text, coordinates, typed input or planner statements', () => {
  const history = new BoundedObservations({ clock });
  history.capture('pixels-one');
  history.actionSucceeded('private secret text and coordinates (1,2)');
  history.capture('pixels-two');
  assert.equal(history.latest.after_action, 'unknown');
  assert.deepEqual(Object.keys(history.latest).sort(), ['after_action', 'base64', 'captured_at', 'sequence']);
  assert.ok(!JSON.stringify(history.observations()).includes('private secret'));
  const copy = history.observations(); copy[0].base64 = 'forged';
  assert.equal(history.observations()[0].base64, 'pixels-one');
});

test('byte budget and reset keep retained pixels and metadata bounded', () => {
  const history = new BoundedObservations({ clock, maxBytes: 30 });
  for (let i = 0; i < 200; i++) {
    history.capture(String(i).padStart(10, 'x'));
    history.actionSucceeded('scroll');
    assert.ok(history.bytes <= 30);
    assert.ok(history.size <= 3);
  }
  assert.equal(history.latest.base64, 'xxxxxxx199');
  assert.throws(() => history.capture('x'.repeat(31)), /budget/);
  history.clear();
  assert.equal(history.size, 0); assert.equal(history.bytes, 0); assert.equal(history.latest, null);
  assert.equal(history.firstNavigation, null); assert.equal(history.lastNavigation, null);
  assert.deepEqual(history.initial, []); assert.equal(history.pending, null);
  history.capture('fresh-task');
  assert.equal(history.latest.sequence, 1); assert.equal(history.latest.after_action, null);
});

test('large encoded 4K-like frames remain within the 24MiB request evidence budget', () => {
  const history = new BoundedObservations({ clock });
  const encodedFrame = 'A'.repeat(4 * 1024 * 1024);
  for (let i = 0; i < 100; i++) {
    history.capture(encodedFrame);
    history.actionSucceeded('scroll');
    assert.ok(history.size <= 6);
    assert.ok(history.bytes <= 24 * 1024 * 1024);
  }
  assert.equal(history.latest.sequence, 100);
  assert.ok(history.summary().omitted_observations >= 94);
  assert.throws(() => history.capture('A'.repeat(24 * 1024 * 1024 + 1)), /budget/);
  assert.equal(history.latest.sequence, 100, 'oversize data cannot silently replace the fresh frame');
});

test('invalid budgets and timestamps cannot weaken retention or create evidence', () => {
  for (const options of [{ maxFrames: 999 }, { maxFrames: 2 }, { maxBytes: Infinity }, { maxBytes: 0 }, { clock: false }]) {
    assert.throws(() => new BoundedObservations(options));
  }
  const history = new BoundedObservations({ clock: () => 'not-a-date' });
  assert.throws(() => history.capture('real-pixels'));
  assert.equal(history.size, 0);
});
