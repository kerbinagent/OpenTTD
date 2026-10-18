/** Incremental, bounded SSE framing. Stream ownership and reconnect live in viewer.js. */
export function createSSEParser(onEvent, { maxBuffer = 65536 } = {}) {
  if (typeof onEvent !== "function" || !Number.isInteger(maxBuffer) || maxBuffer < 1) {
    throw new TypeError("Invalid SSE parser configuration");
  }
  let line = "", data = [], eventType = "", lastId = "", size = 0;
  let afterCR = false, firstCharacter = true;
  function consumeLine() {
    if (!line) {
      if (data.length) onEvent({ type: eventType || "message", data: data.join("\n"), id: lastId });
      data = []; eventType = ""; size = 0;
      return;
    }
    if (line[0] !== ":") {
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") data.push(value);
      else if (field === "event") eventType = value;
      else if (field === "id" && !value.includes("\0")) lastId = value;
    }
    line = "";
  }
  return {
    push(text) {
      for (const character of text) {
        if (firstCharacter) {
          firstCharacter = false;
          if (character === "\uFEFF") continue;
        }
        if (afterCR) {
          afterCR = false;
          if (character === "\n") continue;
        }
        if (++size > maxBuffer) throw new RangeError("SSE frame exceeds buffer limit");
        if (character === "\r" || character === "\n") {
          consumeLine();
          afterCR = character === "\r";
        } else {
          line += character;
        }
      }
    },
    finish() {
      // EOF is not an event delimiter. Never dispatch a partially received event.
      line = ""; data = []; eventType = ""; size = 0; afterCR = false;
    },
  };
}
