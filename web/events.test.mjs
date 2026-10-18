import test from "node:test";
import assert from "node:assert/strict";
import { createSSEParser } from "./events.mjs";

test("frames CRLF, comments and multiline data across every possible chunk boundary", () => {
  const wire = "\uFEFF: heartbeat\r\nid: cursor1\r\nevent: chat.message\r\ndata: first\r\ndata:second\r\n\r\n";
  for (let split = 0; split <= wire.length; split++) {
    const events = [], parser = createSSEParser(event => events.push(event));
    parser.push(wire.slice(0, split)); parser.push(wire.slice(split)); parser.finish();
    assert.deepEqual(events, [{ type: "chat.message", data: "first\nsecond", id: "cursor1" }]);
  }
});

test("persists and resets IDs, ignores NUL IDs, handles CR and empty data", () => {
  const events = [], parser = createSSEParser(event => events.push(event));
  for (const char of "id: one\ndata: a\n\nid: bad\0id\rdata:\r\rid:\ndata: c\n\n") parser.push(char);
  assert.deepEqual(events, [
    { type: "message", data: "a", id: "one" },
    { type: "message", data: "", id: "one" },
    { type: "message", data: "c", id: "" },
  ]);
});

test("ignores empty frames and incomplete EOF without dispatching partial data", () => {
  const events = [], parser = createSSEParser(event => events.push(event));
  parser.push(": comment\n\nevent: ignored\n\nretry: 42\nunknown: field\n\ndata: incomplete\n");
  parser.finish();
  assert.deepEqual(events, []);
});

test("bounds both an unterminated line and accumulated multiline frame", () => {
  assert.throws(() => createSSEParser(() => {}, { maxBuffer: 10 }).push("x".repeat(11)), RangeError);
  const parser = createSSEParser(() => {}, { maxBuffer: 20 });
  parser.push("data: 1\ndata: 2\n");
  assert.throws(() => parser.push("data: 3\n"), RangeError);
});

test("resets the frame budget after a complete event", () => {
  let count = 0;
  const parser = createSSEParser(() => count++, { maxBuffer: 9 });
  parser.push("data: a\n\n".repeat(100));
  assert.equal(count, 100);
});
