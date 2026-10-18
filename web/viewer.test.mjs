import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { createSSEParser } from "./events.mjs";
import * as worldStatusHelpers from "./world-status.mjs";

// Run the actual browser module with a small DOM/fetch boundary. No browser
// dependencies, API server, renderer or native world are needed for ordering.
function viewer(fetch) {
  const nodes = new Map();
  const documentListeners = new Map(), windowListeners = new Map();
  const timers = new Map();
  let nextID = 0;
  function listen(listeners, type, callback) {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(callback);
  }
  function node() {
    const listeners = new Map(), classes = new Set(), attributes = new Map();
    return { textContent: "", value: "", checked: false, hidden: false, children: [], listeners, attributes,
      classList: { toggle(key, force) { const value = force ?? !classes.has(key); if (value) classes.add(key); else classes.delete(key); return value; }, remove(key) { classes.delete(key); }, add(key) { classes.add(key); }, contains(key) { return classes.has(key); } },
      addEventListener(type, callback) { listen(listeners, type, callback); },
      setAttribute(key, value) { attributes.set(key, value); }, removeAttribute(key) { attributes.delete(key); },
      replaceChildren(...children) { this.children = children; }, append(...children) { this.children.push(...children); }, close() {}, focus(options) { this.focused = true; this.focusOptions = options; }, setPointerCapture() {}, releasePointerCapture() {},
      getBoundingClientRect() { return { left: 20.5, top: 30.25, width: 640, height: 400 }; } };
  }
  const context = vm.createContext({ ...worldStatusHelpers, createSSEParser, fetch, AbortController,
    TextDecoder, TextEncoder, DOMException, console,
    setTimeout(callback, delay) { const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay); timers.set(timer, delay); return timer; },
    clearTimeout(timer) { timers.delete(timer); clearTimeout(timer); },
    document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
      querySelectorAll() { return []; }, addEventListener(type, callback) { listen(documentListeners, type, callback); }, createElement: node },
    window: { addEventListener(type, callback) { listen(windowListeners, type, callback); } }, URL, performance,
    crypto: { randomUUID: () => `request-${++nextID}` } });
  const source = readFileSync(new URL("./viewer.js", import.meta.url), "utf8").replace(/^import .*;\n/gm, "");
  vm.runInContext(source, context);
  vm.runInContext('session = {token: "token", me: {company_id: 0}}; generation = 1; pause = async () => {}; renderMessages = () => {};', context);
  function fire(listeners, type, event = {}) {
    return Promise.all((listeners.get(type) || []).map(callback => callback({ preventDefault() {}, ...event })));
  }
  return { context, nodes, timers, run: code => vm.runInContext(code, context),
    fire: (id, type, event) => fire(nodes.get(id).listeners, type, event),
    documentEvent: (type, event) => fire(documentListeners, type, event), windowEvent: (type, event) => fire(windowListeners, type, event) };
}

function snapshot(revision, cursor = "anchor", instance = "engine") {
  return { world_id: "world", world_epoch: "epoch", engine_instance_id: instance,
    pause: { paused: true, reasons: ["manual"], command_policy: "no_actions", revision }, event_cursor: cursor };
}
const json = data => ({ ok: true, json: async () => data, text: async () => JSON.stringify(data) });
test("participants show optional self-reported model and effort as text for each agent", async () => {
  const app = viewer(async path => {
    assert.equal(path, "/v1/participants");
    return json({ participants: [
      { company_id: 1, display_name: "<b>Agent</b>", kind: "agent", model_report: { model_name: "<img src=x>", effort_level: "high", self_reported: true } },
      { company_id: 1, display_name: "Other Agent", kind: "agent" },
      { company_id: 0, display_name: "Human", kind: "human" },
    ] });
  });
  await app.run("refreshParticipants(1)");
  const rows = app.nodes.get("participants").children;
  function texts(node) { return [node.textContent, ...node.children.flatMap(texts)].filter(Boolean); }
  assert.ok(texts(rows[0]).includes("Self-reported model: <img src=x> · Effort: high"));
  assert.ok(texts(rows[1]).includes("Model not reported"));
  assert.equal(texts(rows[2]).some(text => /model/i.test(text)), false);
});
const message = number => ({ message_id: `m${number}`, thread_id: "thread", timestamp: String(number).padStart(6, "0"), body: "hello" });
function stream(text) {
  let pending = true;
  return { ok: true, headers: { get: () => "text/event-stream" }, body: { getReader: () => ({
    async read() { if (!pending) return { done: true }; pending = false; return { value: new TextEncoder().encode(text), done: false }; },
    async cancel() {},
  }) } };
}

test("actual reconnect reads fresh status then resumes the shared cursor and chat tick updates", async () => {
  const calls = [];
  let snapshots = 0, streams = 0, app;
  app = viewer(async (path, options) => {
    calls.push([path, options?.headers?.["Last-Event-ID"]]);
    if (path === "/v1/world/status") {
      if (++snapshots === 3) { app.run("eventAbort.abort()"); throw new DOMException("Stopped", "AbortError"); }
      return json(snapshot(snapshots === 1 ? 1 : 5, `anchor${snapshots}`));
    }
    if (path === "/v1/threads") return json({ threads: [{ thread_id: "thread", scope: "world" }] });
    if (path.startsWith("/v1/threads/thread/messages")) return json({ messages: [], next_after: null });
    if (path === "/v1/events") {
      if (++streams === 1) {
        return stream(`id: chat1\nevent: chat.message\ndata: ${JSON.stringify(message(1))}\n\nid: chat2\nevent: world.status\ndata: ${JSON.stringify((({ event_cursor, ...rest }) => rest)(snapshot(4)))}\n\n`);
      }
      return stream('id: chat3\nevent: chat.message.tick\ndata: {"message_id":"m1","tick":99,"tick_authoritative":true}\n\n');
    }
    throw new Error(`Unexpected ${path}`);
  });
  app.run('threads = [{thread_id: "thread", scope: "world"}]; selectedThread = "thread";');
  await app.run("connectEvents(1)");
  assert.deepEqual(calls.filter(([path]) => path === "/v1/events").map(([, cursor]) => cursor), ["anchor1", "chat2"]);
  const statusIndices = calls.flatMap(([path], index) => path === "/v1/world/status" ? [index] : []);
  const eventIndices = calls.flatMap(([path], index) => path === "/v1/events" ? [index] : []);
  assert.ok(statusIndices[0] < eventIndices[0] && statusIndices[1] < eventIndices[1]);
  assert.equal(app.run('messages.get("m1").tick'), 99);
  assert.equal(app.nodes.get("world-status").textContent, "World status unavailable");
});

test("snapshot recovery catches up history without discarding progress beyond five pages", async () => {
  const after = [];
  let app;
  app = viewer(async path => {
    if (path === "/v1/world/status") return json(snapshot(1));
    if (path === "/v1/threads") return json({ threads: [{ thread_id: "thread", scope: "world" }] });
    if (path.startsWith("/v1/threads/thread/messages")) {
      const previous = new URL(path, "https://test").searchParams.get("after");
      after.push(previous);
      const start = previous ? Number(previous.slice(1)) : 0;
      const messages = Array.from({ length: Math.min(100, 600 - start) }, (_, index) => message(start + index + 1));
      return json({ messages, next_after: messages.at(-1)?.message_id || previous });
    }
    if (path === "/v1/events") { app.run("eventAbort.abort()"); return stream(""); }
    throw new Error(`Unexpected ${path}`);
  });
  app.run('threads = [{thread_id: "thread", scope: "world"}]; selectedThread = "thread";');
  await app.run("connectEvents(1)");
  assert.equal(app.run("historyCursor"), "m500");
  await app.run("refreshHistory(1)");
  assert.equal(app.run("historyCursor"), "m600");
  assert.deepEqual(after, [null, "m100", "m200", "m300", "m400", "m500", "m600"]);
  assert.equal(app.run("messages.size"), 250);
  assert.equal(app.run('messages.has("m600")'), true);
});

test("unavailable native status leaves chat live and cannot promote persisted status", async () => {
  let app, snapshots = 0, streams = 0;
  app = viewer(async path => {
    if (path === "/v1/world/status") {
      if (++snapshots === 1) return { ok: false, status: 503 };
      assert.equal(app.nodes.get("world-status").textContent, "World status unavailable");
      app.run("eventAbort.abort()");
      return { ok: false, status: 503 };
    }
    if (path === "/v1/events") {
      streams++;
      const { event_cursor, ...persisted } = snapshot(9);
      return stream(`id: chat\nevent: chat.message\ndata: ${JSON.stringify(message(1))}\n\nid: status\nevent: world.status\ndata: ${JSON.stringify(persisted)}\n\n`);
    }
    throw new Error(`Unexpected ${path}`);
  });
  app.run('selectedThread = "thread"; threads = [{thread_id: "thread"}];');
  await app.run("connectEvents(1)");
  assert.equal(streams, 1);
  assert.equal(snapshots, 2);
  assert.equal(app.run('messages.has("m1")'), true);
  assert.equal(app.run("worldStatus"), null);
});

for (const recovery of ["history_gap", "instance", "expired"]) {
  test(`actual ${recovery} recovery gets a fresh snapshot and reconciles chat before its new cursor`, async () => {
    const calls = [];
    let snapshots = 0, streams = 0, app;
    app = viewer(async (path, options) => {
      calls.push([path, options?.headers?.["Last-Event-ID"]]);
      if (path === "/v1/world/status") {
        if (++snapshots === 3) { app.run("eventAbort.abort()"); return json(snapshot(3)); }
        return json(snapshot(snapshots, `anchor${snapshots}`, snapshots === 2 && recovery === "instance" ? "engine2" : "engine"));
      }
      if (path === "/v1/threads") return json({ threads: [{ thread_id: "thread", scope: "world" }] });
      if (path.startsWith("/v1/threads/thread/messages")) return json({ messages: [], next_after: "m100" });
      if (path === "/v1/events") {
        if (++streams === 1) {
          if (recovery === "expired") return { ok: false, status: 409 };
          const { event_cursor, ...incoming } = snapshot(1, "", recovery === "instance" ? "engine2" : "engine");
          if (recovery === "history_gap") incoming.history_gap = true;
          return stream(`id: change\nevent: world.status\ndata: ${JSON.stringify(incoming)}\n\n`);
        }
        return stream("");
      }
      throw new Error(`Unexpected ${path}`);
    });
    app.run('selectedThread = "thread"; threads = [{thread_id: "thread"}]; historyCursor = "m100";');
    await app.run("connectEvents(1)");
    assert.deepEqual(calls.filter(([path]) => path === "/v1/events").map(([, cursor]) => cursor), ["anchor1", "anchor2"]);
    const secondStatus = calls.findIndex(([path], index) => path === "/v1/world/status" && index > 0);
    const secondStream = calls.findIndex(([path, cursor]) => path === "/v1/events" && cursor === "anchor2");
    assert.ok(calls.slice(secondStatus + 1, secondStream).some(([path]) => path.includes("messages?limit=100&after=m100")));
    assert.equal(app.run("historyCursor"), "m100");
  });
}

test("an old session's failed snapshot cannot overwrite a new session's current indicator", async () => {
  let reject;
  const app = viewer(() => new Promise((resolve, fail) => { reject = fail; }));
  const pending = app.run("connectEvents(1)");
  app.run('generation = 2; showWorldStatus({pause: {paused: false, reasons: [], command_policy: "all_actions"}});');
  reject(new Error("Old request failed"));
  await pending;
  assert.equal(app.nodes.get("world-status").textContent, "World running");
});

function nativeFrame(app, id = "displayed") {
  app.context.testFrameID = id;
  app.run(`session.me.mode = "visual"; session.capabilities = {mode: "visual", tools: [{name: "ui.apply", target: "image", gestures: ["click", "drag", "text"]}]};
    frame = {id: testFrameID, width: 1280, height: 800}; frameURL = "blob:" + testFrameID; frameRevision++;
    dom["game-image"].hidden = false; dom["game-image"].complete = true; dom["game-image"].currentSrc = frameURL;
    dom["game-image"].naturalWidth = 1280; dom["game-image"].naturalHeight = 800; setViewControls();`);
}

function stubCapture(app) {
  app.run(`var testCaptures = 0; capture = async () => {
    testCaptures++; frame = {id: "fresh-" + testCaptures, width: 1280, height: 800}; frameURL = "blob:" + frame.id;
    dom["game-image"].currentSrc = frameURL; dom["game-image"].complete = true;
  };`);
}
const pointer = (extra = {}) => ({ pointerId: 1, isPrimary: true, button: 0, buttons: 1,
  clientX: 120.5, clientY: 130.25, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...extra });
const flush = () => new Promise(resolve => setImmediate(resolve));
async function gesture(app, down = {}, up = {}) {
  await app.fire("game-image", "pointerdown", pointer(down));
  await app.fire("game-image", "pointerup", pointer({ buttons: 0, ...down, ...up }));
  await flush();
}

test("actual pointer handlers map displayed PNG pixels once, serialize Ctrl drag and stationary right click", async t => {
  const calls = [];
  const app = viewer(async (path, options) => {
    calls.push([path, options]);
    const body = JSON.parse(options.body);
    return json({ request_id: body.request_id, status: "completed" });
  });
  t.after(() => app.run("disconnect()"));
  nativeFrame(app); stubCapture(app);
  app.context.devicePixelRatio = 3;
  await gesture(app);
  await app.fire("game-image", "click", pointer());
  assert.equal(calls.length, 1);
  let body = JSON.parse(calls[0][1].body);
  assert.deepEqual(body.action.parameters, { image_id: "displayed", gesture: "click", x: 200, y: 200, button: "left", ctrl: false });
  await gesture(app, { ctrlKey: true }, { ctrlKey: true, clientX: 320.5, clientY: 230.25 });
  body = JSON.parse(calls[1][1].body);
  assert.deepEqual(body.action.parameters, { image_id: "fresh-1", gesture: "drag", x: 200, y: 200, button: "left", ctrl: true, end_x: 600, end_y: 400 });
  await gesture(app, { button: 2, buttons: 2 }, { button: 2, buttons: 0 });
  assert.equal(JSON.parse(calls[2][1].body).action.parameters.button, "right");
  assert.equal(new Set(calls.map(([, options]) => JSON.parse(options.body).request_id)).size, 3);
  assert.equal(app.run("testCaptures"), 3);
  assert.equal(app.nodes.get("game-stage").focused, true);
  assert.equal(app.nodes.get("game-stage").focusOptions.preventScroll, true);
  await app.fire("game-stage", "keydown", { key: "ArrowRight" }); await flush();
  assert.equal(calls[3][0], "/v1/view/controls");
  assert.deepEqual(JSON.parse(calls[3][1].body), { image_id: "fresh-3", action: "pan", dx: 240, dy: 0 });
});

test("actual pointer cancellation and pinned-frame checks send no request", async t => {
  const calls = [];
  const app = viewer(async (...args) => { calls.push(args); throw new Error("Unexpected request"); });
  t.after(() => app.run("disconnect()"));
  const begin = async () => { nativeFrame(app); await app.fire("game-image", "pointerdown", pointer()); };
  await begin(); await app.fire("game-image", "pointercancel", pointer());
  await begin(); await app.fire("game-image", "lostpointercapture", pointer());
  await begin(); await app.windowEvent("blur");
  await begin(); await app.windowEvent("resize");
  await begin(); await app.fire("game-image", "pointerdown", pointer({ pointerId: 2, isPrimary: false }));
  await begin(); await app.documentEvent("keydown", pointer({ ctrlKey: true }));
  await app.documentEvent("keyup", pointer({ ctrlKey: false }));
  await begin(); app.run('frame = {...frame, id: "replacement"};');
  await app.fire("game-image", "pointerup", pointer({ buttons: 0 }));
  await begin(); app.nodes.get("game-image").currentSrc = "blob:replacement";
  await app.fire("game-image", "pointerup", pointer({ buttons: 0 }));
  await begin(); app.nodes.get("game-image").naturalWidth = 1279;
  await app.fire("game-image", "pointerup", pointer({ buttons: 0 }));
  await begin(); app.nodes.get("game-image").getBoundingClientRect = () => ({ left: 21, top: 30.25, width: 640, height: 400 });
  await app.fire("game-image", "pointerup", pointer({ buttons: 0 }));
  await begin(); await app.fire("game-image", "pointerup", pointer({ buttons: 0, clientX: 9999 }));
  await begin(); await app.documentEvent("visibilitychange", (app.context.document.hidden = true, {}));
  app.context.document.hidden = false;
  await gesture(app, { shiftKey: true });
  await gesture(app, { altKey: true });
  await gesture(app, { metaKey: true });
  nativeFrame(app); await gesture(app, { button: 2, buttons: 2 }, { button: 2, buttons: 0, clientX: 160 });
  assert.equal(calls.length, 0);
  assert.equal(app.run("activePointer"), null);
});

test("pixel bounds exclude stage letterboxing and right/bottom edges", async t => {
  const app = viewer(async () => { throw new Error("Unexpected request"); });
  t.after(() => app.run("disconnect()"));
  nativeFrame(app);
  assert.deepEqual(JSON.parse(app.run("JSON.stringify(imagePoint(imagePin(),20.5,30.25))")), { x: 0, y: 0 });
  assert.equal(app.run("imagePoint(imagePin(),660.5,40)"), null);
  assert.equal(app.run("imagePoint(imagePin(),40,430.25)"), null);
  assert.equal(app.run("imagePoint(imagePin(),20.4,100)"), null);
  app.nodes.get("game-image").getBoundingClientRect = () => ({ left: 1.25, top: 2.5, width: 320, height: 200 });
  assert.deepEqual(JSON.parse(app.run("JSON.stringify(imagePoint(imagePin(),321.249,202.499))")), { x: 1279, y: 799 });
  app.nodes.get("game-image").getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 200 });
  assert.equal(app.run("imagePin()"), null);
});

test("lost POST, GET404 and explicit retry preserve immutable ID/body and camera ownership", async t => {
  const calls = [];
  let posts = 0;
  const app = viewer(async (path, options) => {
    calls.push([path, options]);
    if (options.method === "POST" && ++posts === 1) throw new Error("Lost reply");
    return { ok: false, status: 404, json: async () => ({ error: { code: "image_unavailable", message: "Unavailable" } }) };
  });
  t.after(() => app.run("disconnect()"));
  nativeFrame(app); stubCapture(app);
  await gesture(app);
  const original = calls[0][1].body;
  assert.equal(app.run("pendingInput.status"), "uncertain");
  await app.windowEvent("blur");
  assert.match(app.nodes.get("native-feedback").textContent, /Outcome unconfirmed/);
  await app.fire("input-check", "click"); await flush();
  assert.equal(app.run("pendingInput.status"), "uncertain");
  await app.fire("input-retry", "click"); await flush();
  assert.equal(calls[2][1].body, original);
  assert.equal(app.run("pendingInput.attempts"), 2);
  assert.equal(app.run("pendingInput.status"), "uncertain");
  await app.run('runView({action: "pan",dx:1,dy:0})');
  await gesture(app);
  assert.equal(calls.length, 3);
  assert.equal(app.run("testCaptures"), 0);
});

test("first strict image-unavailable refusal refreshes without an automatic new gesture", async t => {
  const calls = [];
  const app = viewer(async (...args) => {
    calls.push(args);
    return { ok: false, status: 404, json: async () => ({ error: { code: "image_unavailable", message: "Image unavailable" } }) };
  });
  t.after(() => app.run("disconnect()"));
  nativeFrame(app); stubCapture(app);
  await gesture(app);
  assert.equal(app.run("pendingInput"), null);
  assert.equal(app.run("testCaptures"), 1);
  assert.equal(calls.length, 1);
  assert.match(app.nodes.get("native-feedback").textContent, /before input was accepted/);
});

test("pending status checks are GET-only and terminal receipt releases exactly one capture", async t => {
  const calls = [];
  let id;
  const app = viewer(async (path, options) => {
    calls.push([path, options]);
    if (options.method === "POST") { id = JSON.parse(options.body).request_id; return json({ request_id: id, status: "accepted" }); }
    return json({ request_id: id, status: "completed" });
  });
  t.after(() => app.run("disconnect()"));
  nativeFrame(app); stubCapture(app);
  await gesture(app);
  assert.equal(app.run("pendingInput.status"), "accepted");
  assert.equal(await app.run("runView()"), false);
  await app.fire("input-check", "click"); await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[1][0], `/v1/actions/${id}`);
  assert.equal(calls[1][1].method, "GET");
  assert.equal(app.run("pendingInput"), null);
  assert.equal(app.run("testCaptures"), 1);
});

test("IME draft-first targeting commits text only, allows empty text and leaves native OK separate", async t => {
  const calls = [];
  const app = viewer(async (path, options) => {
    const body = JSON.parse(options.body); calls.push(body);
    return json({ request_id: body.request_id, status: "completed" });
  });
  t.after(() => app.run("disconnect()"));
  nativeFrame(app); stubCapture(app);
  await app.fire("type-text", "click");
  app.nodes.get("native-text").value = "Renamed town";
  await app.fire("native-text", "compositionstart");
  await app.fire("apply-native-text", "click");
  assert.equal(app.run("testCaptures"), 0);
  await app.fire("native-text", "compositionend");
  await app.fire("apply-native-text", "click");
  assert.equal(app.run("testCaptures"), 1);
  assert.equal(calls.length, 0);
  await gesture(app);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].action.parameters, { image_id: "fresh-1", gesture: "text", x: 200, y: 200, text: "Renamed town" });
  assert.equal(app.run("inputMode"), "play");
  await app.fire("type-text", "click"); app.nodes.get("native-text").value = "";
  await app.fire("apply-native-text", "click"); await gesture(app);
  assert.equal(calls[1].action.parameters.text, "");
  assert.equal(calls.length, 2); // No synthetic native click or native OK.
});

test("draft mutation during a tracking text pointer cancels rather than sending the old draft", async t => {
  const calls = [];
  const app = viewer(async (...args) => { calls.push(args); throw new Error("Unexpected request"); });
  t.after(() => app.run("disconnect()")); nativeFrame(app); stubCapture(app);
  await app.fire("type-text", "click"); app.nodes.get("native-text").value = "old";
  await app.fire("apply-native-text", "click");
  await app.fire("game-image", "pointerdown", pointer());
  app.nodes.get("native-text").value = "new";
  await app.fire("native-text", "input");
  await app.fire("game-image", "pointerup", pointer({ buttons: 0 }));
  assert.equal(calls.length, 0);
  assert.equal(app.run("activePointer"), null);
  assert.equal(app.run("textArmed"), null);
});

test("capabilities enable visual play independent of identity kind; pause does not blanket-disable it", async t => {
  const app = viewer(async () => { throw new Error("Unexpected request"); });
  t.after(() => app.run("disconnect()")); nativeFrame(app);
  app.run('session.me.kind = "agent"; worldStatus = {pause:{paused:true}}; setViewControls();');
  assert.equal(app.nodes.get("play").disabled, false);
  app.run('session.me.mode = "text"; setViewControls();');
  assert.equal(app.nodes.get("play").disabled, true);
  app.run('session.me.mode = "visual"; session.capabilities = null; setViewControls();');
  assert.equal(app.nodes.get("play").disabled, true);
});

test("Live finally respects text/pointer ownership and Expand cancels unsent targets without resetting chat", async t => {
  const app = viewer(async () => { throw new Error("Unexpected request"); });
  t.after(() => app.run("disconnect()")); nativeFrame(app); stubCapture(app);
  app.nodes.get("live-view").checked = true;
  await app.fire("type-text", "click");
  await app.run("runView()");
  assert.equal([...app.timers.values()].includes(1000), false);
  app.run('selectedThread = "thread";');
  app.nodes.get("native-text").value = "draft";
  await app.fire("apply-native-text", "click");
  assert.ok(app.run("textArmed"));
  await app.fire("expand-view", "click");
  assert.equal(app.run("textArmed"), null);
  assert.equal(app.run("selectedThread"), "thread");
  assert.equal(app.nodes.get("workspace").classList.contains("expanded"), true);
});

test("capture commits identity only after the displayed DOM image decodes and rejects input while busy", async t => {
  const calls = [];
  const app = viewer(async path => {
    calls.push(path);
    if (path === "/v1/view/images") return json({ image_id: "decoded", width: 1280, height: 800 });
    if (path === "/v1/images/decoded") {
      let read = false;
      return { ok: true, headers: { get: key => key === "Content-Type" ? "image/png" : "2" }, body: { getReader: () => ({
        async read() { if (read) return { done: true }; read = true; return { done: false, value: new Uint8Array(2) }; }, async cancel() {},
      }) } };
    }
    throw new Error(`Unexpected ${path}`);
  });
  t.after(() => app.run("disconnect()")); nativeFrame(app);
  app.context.Blob = Blob;
  app.context.Image = class { naturalWidth = 1280; naturalHeight = 800; async decode() {} };
  app.context.URL = { createObjectURL: () => "blob:decoded", revokeObjectURL() {} };
  let finishDecode;
  app.nodes.get("game-image").decode = () => new Promise(resolve => { finishDecode = () => { app.nodes.get("game-image").currentSrc = "blob:decoded"; resolve(); }; });
  const capture = app.run("runView()");
  await flush();
  assert.equal(app.run("frame.id"), "displayed");
  assert.equal(app.run("frameBusy"), true);
  await gesture(app);
  assert.deepEqual(calls, ["/v1/view/images", "/v1/images/decoded"]);
  finishDecode(); await capture;
  assert.equal(app.run("frame.id"), "decoded");
  assert.equal(app.run("frameURL"), "blob:decoded");
  assert.equal(app.run("frameBusy"), false);
});

test("a late old-session POST cannot release or repaint the new session", async t => {
  let resolvePost, id;
  const app = viewer(async (path, options) => {
    id = JSON.parse(options.body).request_id;
    return new Promise(resolve => { resolvePost = resolve; });
  });
  t.after(() => app.run("disconnect()")); nativeFrame(app); stubCapture(app);
  await gesture(app);
  assert.equal(app.run("pendingInput.busy"), true);
  app.run('disconnect(); session = {token:"new-token",me:{company_id:0}}; generation++;');
  nativeFrame(app, "new-session");
  app.nodes.get("native-feedback").textContent = "New session";
  resolvePost(json({ request_id: id, status: "completed" })); await flush();
  assert.equal(app.run("frame.id"), "new-session");
  assert.equal(app.run("pendingInput"), null);
  assert.equal(app.nodes.get("native-feedback").textContent, "New session");
  assert.equal(app.run("testCaptures"), 0);
});

test("malformed receipts and stale refusal after known acceptance stay uncertain", async t => {
  let attempt = 0;
  const app = viewer(async (path, options) => {
    const body = JSON.parse(options.body);
    if (++attempt === 1) return json({ request_id: body.request_id, status: "accepted" });
    return { ok: false, status: 404, json: async () => ({ error: { code: "image_unavailable", message: "Unavailable" } }) };
  });
  t.after(() => app.run("disconnect()")); nativeFrame(app); stubCapture(app);
  await gesture(app);
  await app.fire("input-retry", "click"); await flush();
  assert.equal(app.run("pendingInput.status"), "uncertain");
  assert.equal(app.run("testCaptures"), 0);
  assert.throws(() => app.run('acceptInputReceipt(pendingInput,{request_id:"wrong",status:"completed"})'));
  assert.throws(() => app.run('acceptInputReceipt(pendingInput,{request_id:pendingInput.id,status:"unknown"})'));
  assert.throws(() => app.run('acceptInputReceipt(pendingInput,{request_id:pendingInput.id,status:"completed",extra:true})'));
});

test("delayed cancellation for an old pointer cannot cancel the current sequence", async t => {
  const calls = [];
  const app = viewer(async (path, options) => {
    const body = JSON.parse(options.body); calls.push(body);
    return json({ request_id: body.request_id, status: "completed" });
  });
  t.after(() => app.run("disconnect()")); nativeFrame(app); stubCapture(app);
  await app.fire("game-image", "pointerdown", pointer());
  await app.fire("game-image", "pointercancel", pointer());
  await app.fire("game-image", "pointerdown", pointer({ pointerId: 2 }));
  await app.fire("game-image", "lostpointercapture", pointer());
  await app.fire("game-image", "pointercancel", pointer());
  assert.equal(app.run("activePointer.id"), 2);
  await app.fire("game-image", "pointerup", pointer({ pointerId: 2, buttons: 0 })); await flush();
  assert.equal(calls.length, 1);
});

test("capabilities arriving after the frame enable Play and replace only the unavailable placeholder", async t => {
  let resolveCapabilities;
  const app = viewer(async path => {
    if (path === "/v1/me") return json({ mode: "visual", company_id: 0, display_name: "Player" });
    if (path === "/v1/capabilities") return new Promise(resolve => { resolveCapabilities = resolve; });
    throw new Error(`Unexpected ${path}`);
  });
  t.after(() => app.run("disconnect()")); nativeFrame(app); stubCapture(app);
  app.run('session = null; generation = 0; connectEvents = async () => {}; refreshParticipants = async () => {}; refreshThreads = async () => {}; scheduleChat = () => {};');
  app.nodes.get("token").value = "token";
  await app.fire("connect-form", "submit"); await flush();
  assert.equal(app.run("frame.id"), "fresh-1");
  assert.equal(app.nodes.get("play").disabled, true);
  assert.match(app.nodes.get("native-feedback").textContent, /unavailable/);
  resolveCapabilities(json({ mode: "visual", tools: [{ name: "ui.apply", target: "image", gestures: ["click", "drag", "text"] }] }));
  await flush();
  assert.equal(app.nodes.get("play").disabled, false);
  assert.match(app.nodes.get("native-feedback").textContent, /Click or left-drag/);
  app.nodes.get("native-feedback").textContent = "Input completed. Check its result.";
  app.run("setViewControls()");
  assert.equal(app.nodes.get("native-feedback").textContent, "Input completed. Check its result.");
});
