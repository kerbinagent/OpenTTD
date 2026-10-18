// The finite public pause DTO. Older engines without reviver source support
// accept only exactly representable revisions rather than compare rounded IDs.
const reasons = ["manual", "saving_loading", "joining", "error", "insufficient_clients", "game_script", "link_graph", "other"];
const policies = ["no_actions", "no_construction", "no_landscaping", "all_actions"];
const maxRevision = (1n << 64n) - 1n;

function keys(value, required, optional = []) {
  return value && typeof value === "object" && !Array.isArray(value)
    && required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
}

export function parseWorldStatus(text, snapshot = false) {
  const value = JSON.parse(text, (key, value, context) => {
    if (key !== "revision") return value;
    if (typeof value !== "number") throw new TypeError("Invalid pause revision");
    if (context?.source !== undefined) {
      if (!/^(0|[1-9][0-9]*)$/.test(context.source)) throw new TypeError("Invalid pause revision");
      return BigInt(context.source);
    }
    if (!Number.isSafeInteger(value) || value < 0) throw new TypeError("Unsupported pause revision");
    return BigInt(value);
  });
  const required = ["world_id", "world_epoch", "engine_instance_id", "pause", ...(snapshot ? ["event_cursor"] : [])];
  if (!keys(value, required, snapshot ? [] : ["baseline", "history_gap"])) throw new TypeError("Invalid world status");
  for (const key of ["world_id", "world_epoch", "engine_instance_id"]) {
    if (typeof value[key] !== "string" || !value[key] || value[key].length > 256) throw new TypeError("Invalid world fence");
  }
  if (snapshot && (typeof value.event_cursor !== "string" || !value.event_cursor || value.event_cursor.length > 128)) throw new TypeError("Invalid status cursor");
  if (!snapshot && ["baseline", "history_gap"].some(key => Object.hasOwn(value, key) && typeof value[key] !== "boolean")) throw new TypeError("Invalid status delivery");
  const pause = value.pause;
  if (!keys(pause, ["paused", "reasons", "command_policy", "revision"]) || typeof pause.paused !== "boolean"
      || !policies.includes(pause.command_policy) || typeof pause.revision !== "bigint" || pause.revision < 0n || pause.revision > maxRevision
      || !Array.isArray(pause.reasons) || pause.reasons.length > reasons.length) throw new TypeError("Invalid pause state");
  let previous = -1;
  for (const reason of pause.reasons) {
    const index = reasons.indexOf(reason);
    if (index <= previous) throw new TypeError("Invalid pause reasons");
    previous = index;
  }
  if (pause.paused !== (pause.reasons.length > 0)) throw new TypeError("Inconsistent pause state");
  return value;
}

export function sameWorldFence(left, right) {
  return !!left && !!right && ["world_id", "world_epoch", "engine_instance_id"].every(key => left[key] === right[key]);
}

export function worldStatusDecision(current, incoming) {
  if (incoming.history_gap || !sameWorldFence(current, incoming)) return "refresh";
  return incoming.pause.revision <= current.pause.revision ? "ignore" : "apply";
}

export function statusStreamCursor(current, snapshot, previousCursor, refresh = false) {
  // A status snapshot must never jump over unread chat on an ordinary reconnect.
  return !refresh && previousCursor && sameWorldFence(current, snapshot) ? previousCursor : snapshot.event_cursor;
}

const reasonLabels = {
  manual: "manual pause", saving_loading: "saving or loading", joining: "player joining", error: "game error",
  insufficient_clients: "waiting for players", game_script: "game script", link_graph: "cargo distribution", other: "other pause",
};
const policyLabels = {
  no_actions: "Most gameplay commands are blocked while paused; some settings may still be allowed.",
  no_construction: "Construction and landscaping are blocked while paused; other commands may be allowed.",
  no_landscaping: "Landscaping is blocked while paused; other commands may be allowed.",
  all_actions: "Gameplay commands may run while paused, subject to normal game rules.",
};

export function pauseDescription(state) {
  const pause = state.pause;
  return {
    label: pause.paused ? "World paused" : "World running",
    detail: `${pause.paused ? `${pause.reasons.map(reason => reasonLabels[reason]).join(", ")}. ` : ""}${policyLabels[pause.command_policy]}`,
  };
}
