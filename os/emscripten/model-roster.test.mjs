import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const shell = readFileSync(new URL('./shell.html', import.meta.url), 'utf8');
const source = shell.match(/<script id="model-roster-script">([\s\S]*?)<\/script>/)[1];
const json = participants => ({ ok: true, json: async () => ({ participants }) });
const agent = (name = 'Agent', report = undefined) => ({ company_id: 1, display_name: name, kind: 'agent', model_report: report });
const report = { model_name: 'Example Model', effort_level: 'high', self_reported: true };
const settle = () => new Promise(resolve => setImmediate(resolve));

function panel(fetch, enabled = true) {
  const timers = new Map();
  let nextTimer = 0;
  function node() {
    return { hidden: true, open: false, textContent: '', children: [], listeners: new Map(),
      replaceChildren(...children) { this.children = children; },
      addEventListener(type, callback) { this.listeners.set(type, callback); },
      set innerHTML(value) { throw new Error('Untrusted content must use textContent'); } };
  }
  const nodes = new Map(['agents', 'agents-list', 'agents-status'].map(id => [id, node()]));
  vm.runInNewContext(source, {
    fetch, AbortController, document: { getElementById: id => nodes.get(id), createElement: node },
    window: { OPENTTD_BROWSER_RELAY: { modelRoster: enabled } },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  return { nodes, timers,
    async toggle(open) { nodes.get('agents').open = open; nodes.get('agents').listeners.get('toggle')?.(); await settle(); },
    async timer(delay) { const entry = [...timers].find(([, value]) => value.delay === delay); assert.ok(entry, `Missing ${delay}ms timer`); timers.delete(entry[0]); await entry[1].callback(); await settle(); },
    rows: () => nodes.get('agents-list').children.map(row => row.textContent),
    status: () => nodes.get('agents-status').textContent,
  };
}

test('model reports are optional and show submitted labels without inherited slot names', async () => {
  let calls = 0;
  const disabled = panel(async () => { calls++; }, false);
  assert.equal(disabled.nodes.get('agents').hidden, true);
  await disabled.toggle(true);
  assert.equal(calls, 0);
  const app = panel(async (path, options) => {
    calls++;
    assert.equal(path, '/model-roster');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.headers, undefined);
    return json([agent('<img onerror=bad()>', { ...report, model_name: '<script>bad()</script>' }), agent('Other actor'),
      { company_id: 0, display_name: 'Human', kind: 'human' },
      { company_id: null, display_name: 'Spectating Agent', kind: 'agent' }]);
  });
  assert.equal(calls, 0);
  assert.equal(app.nodes.get('agents').hidden, false);
  await app.toggle(true);
  assert.equal(calls, 1);
  assert.deepEqual(app.rows(), [
    'Company #2 · Model: <script>bad()</script> · Effort: high (self-reported)',
  ]);
  assert.match(app.status(), /self-reported/);
  assert.match(app.status(), /does not indicate.*currently connected/);
  assert.match(shell, /<summary>Model reports<\/summary>/);
  assert.match(shell, /id="agents-status" role="status" aria-live="polite"/);
  await app.toggle(false);
  assert.equal(app.timers.size, 0);
  assert.deepEqual(app.rows(), []);
});

test('unused slots and malformed or missing reports do not imply joined agents', async () => {
  const app = panel(async () => json([agent('Luna text competitor'),
    agent('Not self-reported', { ...report, self_reported: false }),
    agent('Incomplete', { model_name: 'Example', self_reported: true }),
    agent('Blank', { ...report, effort_level: ' ' }), null]));
  await app.toggle(true);
  assert.deepEqual(app.rows(), []);
  assert.match(app.status(), /No model reports submitted/);
  assert.match(app.status(), /does not indicate.*currently connected/);
});

test('single-flight polling refreshes reports, clears stale data on failure and stops while closed', async () => {
  let finish, calls = 0;
  const app = panel(async () => {
    calls++;
    if (calls === 1) return new Promise(resolve => { finish = resolve; });
    if (calls === 3) return { ok: false };
    return json([agent('Agent', { ...report, effort_level: calls === 4 ? 'low' : 'high' })]);
  });
  await app.toggle(true);
  await app.toggle(true);
  assert.equal(calls, 1);
  await app.toggle(false);
  finish(json([agent('Old', report)]));
  await settle();
  assert.deepEqual(app.rows(), []);
  assert.equal(app.timers.size, 0);
  await app.toggle(true);
  assert.match(app.rows()[0], /Effort: high/);
  await app.timer(15000);
  assert.deepEqual(app.rows(), []);
  assert.match(app.status(), /unavailable/);
  await app.timer(15000);
  assert.match(app.rows()[0], /Effort: low/);
  await app.toggle(false);
  assert.equal(app.timers.size, 0);
});

test('Agents keyboard events preserve summary defaults without reaching native window handlers', () => {
  const app = panel(async () => { throw new Error('Keyboard boundary must not fetch reports'); });
  const target = app.nodes.get('agents');
  let stopped = 0, prevented = 0;
  for (const type of ['keydown', 'keyup', 'keypress']) {
    const listener = target.listeners.get(type);
    assert.equal(typeof listener, 'function');
    listener({ stopPropagation() { stopped++; }, preventDefault() { prevented++; } });
  }
  assert.equal(stopped, 3);
  assert.equal(prevented, 0);
  assert.equal(target.open, false);
  assert.equal(app.timers.size, 0);
  const disabled = panel(async () => {}, false);
  assert.equal(disabled.nodes.get('agents').listeners.size, 0);
});

test('an unresponsive report request is aborted and becomes unavailable', async () => {
  const app = panel((path, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  await app.toggle(true);
  await app.timer(3000);
  assert.deepEqual(app.rows(), []);
  assert.match(app.status(), /unavailable/);
  assert.equal([...app.timers.values()].filter(timer => timer.delay === 15000).length, 1);
});
