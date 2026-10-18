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
  function node() {
    return { textContent: "", value: "", checked: false, children: [], classList: { toggle() {}, remove() {}, add() {} },
      addEventListener() {}, setAttribute() {}, removeAttribute() {}, replaceChildren() {}, append() {}, close() {} };
  }
  const context = vm.createContext({ ...worldStatusHelpers, createSSEParser, fetch, AbortController,
    TextDecoder, TextEncoder, DOMException, setTimeout, clearTimeout, console,
    document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
      querySelectorAll() { return []; }, addEventListener() {}, createElement: node },
    window: { addEventListener() {} }, URL, performance, crypto: { randomUUID: () => "request" } });
  const source = readFileSync(new URL("./viewer.js", import.meta.url), "utf8").replace(/^import .*;\n/gm, "");
  vm.runInContext(source, context);
  vm.runInContext('session = {token: "token", me: {company_id: 0}}; generation = 1; pause = async () => {}; renderMessages = () => {};', context);
  return { context, nodes, run: code => vm.runInContext(code, context) };
}

function snapshot(revision, cursor = "anchor", instance = "engine") {
  return { world_id: "world", world_epoch: "epoch", engine_instance_id: instance,
    pause: { paused: true, reasons: ["manual"], command_policy: "no_actions", revision }, event_cursor: cursor };
}
const json = data => ({ ok: true, json: async () => data, text: async () => JSON.stringify(data) });
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
