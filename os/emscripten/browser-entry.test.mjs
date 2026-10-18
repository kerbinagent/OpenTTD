import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./browser-entry.js', import.meta.url), 'utf8');
function library() { const env = {}; vm.runInNewContext(source, env); return env.OpenTTDBrowser; }
const key = 'ab'.repeat(32);
function filesystem(files = {}) {
  const data = new Map(Object.entries(files)), writes = [];
  return {data, writes, readFile(path) { if (!data.has(path)) throw {errno: 44}; return data.get(path); },
    writeFile(path, value) { writes.push(path); data.set(path, value); }};
}
const crypto = {getRandomValues(bytes) { bytes.fill(42); return bytes; }};

test('identity exists durably before native startup and preserves an existing modern key', () => {
  const api = library(), fs = filesystem({'/game/openttd.cfg': '[version]\nini_version = 8\n[gui]\nfoo = bar\n',
    '/game/secrets.cfg': '[network]\nclient_secret_key = "' + key + '"\nother = keep\n'});
  api.prepareIdentity(fs, {getRandomValues() { throw Error('must retain key'); }}, '/game');
  assert.deepEqual(fs.writes, []);
  assert.match(fs.data.get('/game/openttd.cfg'), /foo = bar/);
});

test('legacy first-key semantics, no version bump or duplicate key; unrelated content survives', () => {
  const api = library(), fs = filesystem({'/game/openttd.cfg': '; keep\n[network]\nclient_secret_key = invalid\nclient_secret_key = ' + key + '\nother = yes\n[gui]\nfoo = bar\n'});
  api.prepareIdentity(fs, crypto, '/game');
  const config = fs.data.get('/game/openttd.cfg');
  assert.equal(api.iniSetting(config, 'network', 'client_secret_key').value, '2a'.repeat(32));
  assert.match(config, /; keep/); assert.match(config, /other = yes/); assert.match(config, /foo = bar/);
  assert.equal(config.includes('ini_version'), false);
  assert.equal(fs.data.has('/game/secrets.cfg'), false);
  assert.equal(config.match(/client_secret_key/g).length, 2);
});

test('fresh storage receives modern config and secret; failed reads are not replaced', () => {
  const api = library(), fs = filesystem();
  api.prepareIdentity(fs, crypto, '/game');
  assert.match(fs.data.get('/game/openttd.cfg'), /ini_version = 8/);
  assert.equal(api.iniSetting(fs.data.get('/game/secrets.cfg'), 'network', 'client_secret_key').value, '2a'.repeat(32));
  assert.throws(() => api.prepareIdentity({readFile() { throw Error('storage denied'); }}, crypto, '/game'), /storage denied/);
});

test('native quoted/bare/space-separated keys and numeric version prefixes select the same secret', () => {
  const api = library();
  for (const line of ['client_secret_key', '"client_secret_key" = invalid', 'client_secret_key invalid']) {
    const fs = filesystem({'/game/openttd.cfg': '[version]\nini_version = 1suffix\n', '/game/secrets.cfg': '[network]\n' + line + '\nkeep = yes\n'});
    api.prepareIdentity(fs, crypto, '/game');
    assert.equal(api.iniSetting(fs.data.get('/game/secrets.cfg'), 'network', 'client_secret_key').value, '2a'.repeat(32));
    assert.equal(fs.data.get('/game/secrets.cfg').match(/client_secret_key/g).length, 1);
    assert.equal(fs.writes.includes('/game/openttd.cfg'), false);
  }
  const legacy = filesystem({'/game/openttd.cfg': '[version]\nini_version = 65536\n[network]\nclient_secret_key = ' + key + '\n'});
  api.prepareIdentity(legacy, {getRandomValues() { throw Error('must retain legacy key'); }}, '/game');
  assert.equal(legacy.writes.length, 0);
});

test('cookie needs current world, correct port and a real company slot', () => {
  const api = library(), cookie = (world, company, port = '18087') => 'openttd_company_' + port + '=' + encodeURIComponent(JSON.stringify({world, company}));
  assert.equal(api.remembered(cookie('epoch', 0), '18087', 'epoch'), 0);
  for (const value of [cookie('old', 0), cookie('epoch', 255), cookie('epoch', '0'), cookie('epoch', -1), cookie('epoch', 0, '1'), 'openttd_company_18087=broken'])
    assert.equal(api.remembered(value, '18087', 'epoch'), null);
  assert.equal(api.remembered(cookie('epoch', 0), '18087', null), null);
});

function app({cookie = '', hash = '', available = true, protocol = 'http:', webLocks = true, clipboard = true} = {}) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {hidden: true, value: id === 'agent-mode' ? 'text' : 'Make revenue', textContent: '', listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; }, focus() {}, select() {}});
    return nodes.get(id);
  }
  node('welcome').hidden = false;
  const document = {cookie, getElementById: node, execCommand(command) {state.copies.push(command); return true;}};
  const state = {loads: 0, copies: [], locks: 0, document, replaced: 0};
  const env = {document, location: {protocol, port: '18087', hash, pathname: '/', search: '', reload() {}}, history: {replaceState() {state.replaced++;}}, console,
    navigator: {locks: {request(name, options, callback) { state.locks++; callback(available ? {} : null); return new Promise(() => {}); }},
      clipboard: {async writeText(value) { state.copies.push(value); }}}};
  env.window = env;
  if (!webLocks) delete env.navigator.locks;
  if (!clipboard) delete env.navigator.clipboard;
  vm.runInNewContext(source, env);
  env.OpenTTDBrowser.init({worldId: 'epoch', apiOrigin: 'http://localhost:18160'}, () => state.loads++);
  return {...state, state, env, nodes, click: id => node(id).listeners.click()};
}

test('landing and agent prompt never load WASM or allocate a native session', async () => {
  const a = app();
  assert.equal(a.state.loads, 0); assert.equal(a.state.locks, 0);
  a.click('choose-agent');
  assert.match(a.nodes.get('agent-prompt').value, /^Read http:\/\/localhost:18160\/llms.txt/);
  a.nodes.get('agent-mode').value = 'capture'; a.nodes.get('agent-mode').listeners.change();
  await a.click('copy-agent-prompt');
  assert.match(a.state.copies[0], /llms-capture.txt/);
  assert.equal(a.state.loads, 0); assert.equal(a.state.locks, 0);
});

test('observer is immutable read-only and never creates or remembers a company', () => {
  const a = app(); a.click('choose-observe');
  assert.equal(a.env.OPENTTD_BROWSER_SESSION.readOnly, true);
  assert.equal(a.state.loads, 1);
  a.env.openttd_browser_company(0);
  assert.equal(a.document.cookie, '');
  assert.match(a.nodes.get('session-label').textContent, /Observer/);
  assert.equal(Object.getOwnPropertyDescriptor(a.env, 'OPENTTD_BROWSER_SESSION').writable, false);
});

test('human creation remembers confirmed assignment and reload resumes; chooser can bypass it', () => {
  const a = app(); a.click('new-company');
  a.env.openttd_browser_company(255); assert.equal(a.document.cookie, '');
  a.env.openttd_browser_company(4);
  const cookie = a.document.cookie;
  assert.match(cookie, /SameSite=Strict/);
  const b = app({cookie}); assert.equal(b.state.loads, 1);
  const c = app({cookie, hash: '#choose'}); assert.equal(c.state.loads, 0);
  c.click('existing-company'); c.env.openttd_browser_company(255);
  assert.equal(c.state.replaced, 1);
  assert.match(c.document.cookie, /Max-Age=0/);
  const d = app({cookie, hash: '#choose'}); d.click('choose-observe'); assert.equal(d.state.replaced, 0);
  a.env.openttd_browser_company(255);
  assert.match(a.document.cookie, /Max-Age=0/);
});

test('missing company clears remembered selection and gives an explicit restart without another creation', () => {
  const a = app(); a.click('new-company'); a.env.openttd_browser_company(3);
  a.env.openttd_browser_join_failed();
  assert.match(a.document.cookie, /Max-Age=0/);
  assert.equal(a.nodes.get('welcome').hidden, false);
  assert.equal(a.nodes.get('retry-entry').hidden, false);
  assert.equal(a.nodes.get('new-company').disabled, true);
  assert.equal(a.state.loads, 1);
});

test('a second tab cannot load native client or create another identity', () => {
  const a = app({available: false}); a.click('new-company');
  assert.equal(a.state.loads, 0);
  assert.match(a.nodes.get('welcome-status').textContent, /another tab/);
});

test('HTTPS company cookies are Secure on assignment and removal', () => {
  const a = app({protocol: 'https:'}); a.click('new-company'); a.env.openttd_browser_company(3);
  assert.match(a.document.cookie, /; Secure$/);
  a.env.openttd_browser_company(255); assert.match(a.document.cookie, /Max-Age=0; SameSite=Strict; Secure$/);
});

const pre = readFileSync(new URL('./pre.js', import.meta.url), 'utf8');
function bootstrap({failure = null} = {}) {
  const counts = {ready: 0, failed: 0, syncs: 0};
  const env = {Module: {arguments: [], preRun: [], addRunDependency() {}, removeRunDependency() {counts.ready++;}},
    location: {protocol: 'https:', hostname: 'game.example.ts.net', host: 'game.example.ts.net:8443', origin: 'https://game.example.ts.net:8443'},
    FS: {mkdir() {}, mount() {}, syncfs(read, callback) {counts.syncs++; callback(failure === (read ? 'read' : 'write') ? Error('blocked') : null);}},
    IDBFS: {}, document: {addEventListener() {}}, SOCKFS: {websocket_sock_ops: {createPeer() {}}},
    window: {OPENTTD_BROWSER_RELAY: {nativePort: 55029, company: 1, websocketPath: '/native', publicOrigin: 'https://game.example.ts.net:8443'},
      OpenTTDBrowser: {prepareIdentity() {return key;}, persistIdentity() {return failure === 'identity' ? Promise.reject(Error('blocked')) : Promise.resolve(key);}}, openttd_browser_storage_failed() {counts.failed++;}}};
  vm.runInNewContext(pre, env);
  return {env, counts};
}

test('HTTPS bootstrap keeps the native relay fixed and uses WSS', () => {
  const {env} = bootstrap();
  assert.ok(env.Module.arguments.includes('127.0.0.1:55029#1'));
  assert.ok(env.Module.arguments.includes('/home/web_user/.openttd/openttd.cfg'));
  assert.equal(env.Module.websocket.url('127.0.0.1', 55029, 'tcp'), 'wss://game.example.ts.net:8443/native');
  assert.equal(env.Module.websocket.url('elsewhere', 55029, 'tcp'), 'wss://game.example.ts.net:8443/denied-native-destination');
});

test('native startup waits for identity flush, and storage failures leave startup blocked', async () => {
  const a = bootstrap(); a.env.Module.preRun[0]();
  await new Promise(setImmediate);
  assert.equal(a.counts.syncs, 2); assert.equal(a.counts.ready, 1); assert.equal(a.counts.failed, 0);
  for (const failure of ['read', 'write', 'identity']) {
    const b = bootstrap({failure}); b.env.Module.preRun[0]();
    await new Promise(setImmediate);
    assert.equal(b.counts.ready, 0); assert.equal(b.counts.failed, 1);
  }
});

test('HTTP without Web Locks starts normally and copy uses its synchronous fallback', async () => {
  const a = app({webLocks: false, clipboard: false});
  a.click('choose-agent'); await a.click('copy-agent-prompt');
  assert.deepEqual(a.state.copies, ['copy']);
  assert.match(a.nodes.get('welcome-status').textContent, /Prompt copied/);
  a.click('new-company'); assert.equal(a.state.loads, 1); assert.equal(a.state.locks, 0);
});

test('canonical identity replaces a different valid filesystem identity before startup', () => {
  const api = library(), fs = filesystem({'/game/openttd.cfg': '[version]\nini_version = 8\n', '/game/secrets.cfg': '[network]\nclient_secret_key = ' + key + '\n'});
  assert.equal(api.prepareIdentity(fs, crypto, '/game', 'cd'.repeat(32)), 'cd'.repeat(32));
  assert.equal(api.iniSetting(fs.data.get('/game/secrets.cfg'), 'network', 'client_secret_key').value, 'cd'.repeat(32));
});

function database(initial, {abort = false, broken = false} = {}) {
  let stored = initial, active = false;
  const queue = [];
  function drain() { if (active || !queue.length) return; active = true; setImmediate(queue.shift()); }
  return {open() {
    const db = {close() {}, transaction() {
      if (broken) throw Error('missing store');
      let proposed, cancelled = false;
      const tx = {abort() {cancelled = true;}, objectStore() {return {
        add(value) {proposed = value;},
        get() {const request = {}; queue.push(() => {
          request.result = stored; request.onsuccess();
          setImmediate(() => {if (abort || cancelled) tx.onabort(); else {if (proposed !== undefined) stored = proposed; tx.oncomplete();} active = false; drain();});
        }); drain(); return request;}
      };}};
      return tx;
    }};
    const request = {result: db}; setImmediate(() => request.onsuccess()); return request;
  }};
}

test('concurrent first-use transactions choose one durable identity and preserve an existing one', async () => {
  const api = library(), idb = database(undefined);
  const [a, b] = await Promise.all([api.persistIdentity(idb, key), api.persistIdentity(idb, 'cd'.repeat(32))]);
  assert.equal(a, key); assert.equal(b, key);
  assert.equal(await api.persistIdentity(idb, 'ef'.repeat(32)), key);
});

test('invalid canonical identity, transaction abort and malformed store fail closed', async () => {
  const api = library();
  for (const idb of [database('corrupt'), database(undefined, {abort: true}), database(undefined, {broken: true})])
    await assert.rejects(api.persistIdentity(idb, key), /unavailable/);
});
