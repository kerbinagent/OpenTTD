import test from "node:test";
import assert from "node:assert/strict";
import { parseWorldStatus, worldStatusDecision, statusStreamCursor, pauseDescription } from "./world-status.mjs";

function wire(revision = "1", extra = {}, pause = {}) {
  return JSON.stringify({ world_id: "world", world_epoch: "epoch", engine_instance_id: "engine",
    pause: { paused: true, reasons: ["manual"], command_policy: "no_actions", revision: 0, ...pause }, ...extra })
    .replace('"revision":0', `"revision":${revision}`);
}

test("compares uint64 revisions exactly and ignores duplicate or older same-fence status", () => {
  const current = parseWorldStatus(wire("9007199254740992"));
  const next = parseWorldStatus(wire("9007199254740993"));
  assert.equal(worldStatusDecision(current, next), "apply");
  assert.equal(worldStatusDecision(next, current), "ignore");
  assert.equal(worldStatusDecision(next, next), "ignore");
  assert.equal(parseWorldStatus(wire("18446744073709551615")).pause.revision, (1n << 64n) - 1n);
});

test("older JSON engines fail closed for revisions they cannot represent exactly", () => {
  const parse = JSON.parse;
  JSON.parse = (text, reviver) => parse(text, (key, value) => reviver(key, value));
  try {
    assert.equal(parseWorldStatus(wire("42")).pause.revision, 42n);
    assert.throws(() => parseWorldStatus(wire("9007199254740993")));
  } finally { JSON.parse = parse; }
});

test("rejects malformed pause state instead of presenting an unpaused default", () => {
  for (const revision of ["-1", "1.5", "1e2", '"1"', "null", "true", "18446744073709551616"]) {
    assert.throws(() => parseWorldStatus(wire(revision)));
  }
  for (const pause of [{ paused: false }, { reasons: [] }, { reasons: ["private"] },
    { reasons: ["manual", "manual"] }, { reasons: ["joining", "manual"] }, { command_policy: "admin" }, { raw_flags: 8 }]) {
    assert.throws(() => parseWorldStatus(wire("1", {}, pause)));
  }
  assert.throws(() => parseWorldStatus(wire("1", { tick: 4 })));
  assert.throws(() => parseWorldStatus(wire("1", { event_cursor: "anchor" })));
  assert.throws(() => parseWorldStatus(wire("1"), true));
});

test("new world/epoch/instance and history gaps require a fresh snapshot", () => {
  const current = parseWorldStatus(wire());
  for (const extra of [{ world_id: "other" }, { world_epoch: "new" }, { engine_instance_id: "new" }, { history_gap: true }]) {
    assert.equal(worldStatusDecision(current, parseWorldStatus(wire("0", extra))), "refresh");
  }
  assert.equal(worldStatusDecision(current, parseWorldStatus(wire("1", { baseline: true }))), "ignore");
});

test("accepts the native/backend 256-character fences and retains the 128-character cursor limit", () => {
  for (const key of ["world_id", "world_epoch", "engine_instance_id"]) {
    for (const length of [129, 256]) assert.equal(parseWorldStatus(wire("1", { [key]: "x".repeat(length) }))[key].length, length);
    assert.throws(() => parseWorldStatus(wire("1", { [key]: "x".repeat(257) })));
  }
  assert.equal(parseWorldStatus(wire("1", { event_cursor: "x".repeat(128) }), true).event_cursor.length, 128);
  assert.throws(() => parseWorldStatus(wire("1", { event_cursor: "x".repeat(129) }), true));
});

test("ordinary reconnect retains shared chat cursor; recovery uses the snapshot anchor", () => {
  const current = parseWorldStatus(wire());
  const snapshot = parseWorldStatus(wire("2", { event_cursor: "latest" }), true);
  assert.equal(statusStreamCursor(current, snapshot, "unread-chat"), "unread-chat");
  assert.equal(statusStreamCursor(current, snapshot, "unread-chat", true), "latest");
  assert.equal(statusStreamCursor(null, snapshot, ""), "latest");
  assert.equal(statusStreamCursor(current, { ...snapshot, world_epoch: "new" }, "old"), "latest");
});

test("labels all finite policies and pause reasons without adding controls", () => {
  const value = parseWorldStatus(wire("1", {}, { reasons: ["manual", "saving_loading", "joining", "error", "insufficient_clients", "game_script", "link_graph", "other"] }));
  assert.equal(pauseDescription(value).label, "World paused");
  assert.match(pauseDescription(value).detail, /manual pause.*saving or loading.*player joining/);
  for (const policy of ["no_actions", "no_construction", "no_landscaping", "all_actions"]) {
    const state = parseWorldStatus(wire("1", {}, { paused: false, reasons: [], command_policy: policy }));
    assert.equal(pauseDescription(state).label, "World running");
    assert.match(pauseDescription(state).detail, /while paused/);
  }
});
