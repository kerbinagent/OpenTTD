import { createSSEParser } from "/events.mjs";
import { parseWorldStatus, sameWorldFence, worldStatusDecision, statusStreamCursor, pauseDescription } from "/world-status.mjs";

const $ = id => document.getElementById(id);
const dom = Object.fromEntries([
  "connect-panel", "connect-form", "token", "connect", "connect-status", "disconnect",
  "connection-dot", "session-label", "participants", "participant-count", "players-status",
  "view-status", "game-stage", "game-image", "view-empty", "capture-indicator", "inspect",
  "refresh-view", "live-view", "report-window", "open-window", "close-windows", "view-note",
  "thread-select", "mute-thread", "thread-description", "messages", "message-form",
  "message-body", "send-message", "message-feedback", "chat-status", "report-dialog",
  "report-form", "report-reason", "report-feedback", "cancel-report", "world-status", "world-status-detail",
].map(id => [id, $(id)]));

// One session, one camera, bounded chat state. Credentials never leave memory.
let session = null;
let generation = 0;
let frame = null;
let frameURL = null;
let frameAbort = null;
let frameBusy = false;
let frameRevision = 0;
let frameTimer = null;
let nextCaptureAt = 0;
let inspectMode = false;
let threads = [];
let selectedThread = null;
let threadRevision = 0;
let historyAbort = null;
let historyCursor = null;
let historyBusy = false;
let messages = new Map();
let chatTimer = null;
let threadRefreshBusy = false;
let threadRefreshTask = null;
let historyTask = null;
let eventAbort = null;
let cursor = "";
let worldStatus = null;
let reportMessage = null;
let pendingMessage = null;
let blocked = new Set();
const MAX_MESSAGES = 250;

class APIError extends Error {
  constructor(status) { super("Request unavailable"); this.status = status; }
}

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}

async function request(path, { method = "GET", body, signal, raw = false, decode = null, token = session?.token, headers = {} } = {}) {
  if (!token) throw new APIError(401);
  const response = await fetch(path, {
    method, signal, credentials: "omit", cache: "no-store", redirect: "error",
    headers: { Authorization: `Bearer ${token}`, Accept: raw ? "image/png" : "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new APIError(response.status);
  return raw ? response : decode ? decode(await response.text()) : response.json();
}

function current(epoch) { return session && epoch === generation; }
function stopped(error) { return error?.name === "AbortError"; }
function status(node, text, ready = false) { node.textContent = text; node.classList.toggle("ready", ready); }
function companyLabel(company, name) {
  if (company === null || company === undefined || Number(company) === 255) return "Spectator";
  return name || `Company ${Number(company) + 1}`;
}
function requestID() { return crypto.randomUUID(); }

function showWorldStatus(value = null) {
  const description = value ? pauseDescription(value) : { label: session ? "World status unavailable" : "World status offline", detail: session ? "Checking the current state again when the connection returns." : "Connect to see the shared pause state." };
  status(dom["world-status"], description.label, !!value && !value.pause.paused);
  dom["world-status"].classList.toggle("paused", !!value?.pause.paused);
  dom["world-status-detail"].textContent = description.detail;
}

function setViewControls() {
  const canView = !!session;
  const canControl = canView && !!frame && !frameBusy;
  document.querySelectorAll("[data-pan],[data-zoom]").forEach(button => { button.disabled = !canControl; });
  dom.inspect.disabled = !canControl;
  dom["open-window"].disabled = !canControl;
  dom["close-windows"].disabled = !canControl;
  dom["report-window"].disabled = !canControl;
  dom["refresh-view"].disabled = !canView || frameBusy;
  dom["live-view"].disabled = !canView;
  dom["capture-indicator"].hidden = !frameBusy;
  dom.inspect.setAttribute("aria-pressed", String(inspectMode));
  dom["game-stage"].classList.toggle("inspecting", inspectMode && canControl);
}

function clearFrame() {
  frame = null;
  dom["game-image"].hidden = true;
  dom["game-image"].removeAttribute("src");
  dom["view-empty"].hidden = false;
  if (frameURL) URL.revokeObjectURL(frameURL);
  frameURL = null;
}

function disconnect() {
  generation++;
  session = null;
  frameRevision++;
  threadRevision++;
  frameAbort?.abort(); historyAbort?.abort(); eventAbort?.abort();
  clearTimeout(frameTimer); clearTimeout(chatTimer);
  frameBusy = false; historyBusy = false; threadRefreshBusy = false;
  historyTask = null; threadRefreshTask = null;
  nextCaptureAt = 0;
  cursor = ""; selectedThread = null; threads = []; messages.clear(); historyCursor = null;
  worldStatus = null;
  pendingMessage = null; blocked.clear(); reportMessage = null; inspectMode = false;
  clearFrame();
  dom["report-dialog"].close();
  dom.token.value = "";
  dom["message-body"].value = "";
  dom["connect-panel"].hidden = false;
  dom.disconnect.hidden = true;
  dom["connection-dot"].classList.remove("connected");
  dom["session-label"].textContent = "Local session";
  dom.participants.replaceChildren();
  dom["participant-count"].textContent = "0";
  dom["players-status"].textContent = "Connect to see this world's players.";
  dom["thread-select"].replaceChildren(element("option", "", "Connect to chat"));
  dom["thread-select"].disabled = true;
  dom["mute-thread"].disabled = true;
  dom["message-body"].disabled = true;
  dom["send-message"].disabled = true;
  dom["message-feedback"].textContent = "";
  status(dom["chat-status"], "Offline");
  status(dom["view-status"], "Waiting for session");
  showWorldStatus();
  renderMessages();
  setViewControls();
}

dom["connect-form"].addEventListener("submit", async event => {
  event.preventDefault();
  const token = dom.token.value.trim();
  if (!token || session) return;
  const epoch = ++generation;
  dom.connect.disabled = true;
  dom["connect-status"].textContent = "Connecting…";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const me = await request("/v1/me", { token, signal: controller.signal });
    if (epoch !== generation) return;
    session = { token, me, capabilities: null };
    dom.token.value = "";
    dom["connect-panel"].hidden = true;
    dom.disconnect.hidden = false;
    dom["connection-dot"].classList.add("connected");
    dom["session-label"].textContent = `${me.display_name || "Player"} · ${companyLabel(me.company_id)} · ${me.mode || "session"}`;
    dom["connect-status"].textContent = "";
    setViewControls();
    void refreshParticipants(epoch);
    void refreshThreads(epoch);
    void request("/v1/capabilities").then(capabilities => {
      if (current(epoch)) session.capabilities = capabilities;
    }).catch(() => { /* View and chat retain independent availability. */ });
    void connectEvents(epoch);
    void runView();
    scheduleChat(epoch);
  } catch {
    if (epoch === generation) dom["connect-status"].textContent = "Could not connect. Check your token and that the world service is running.";
  } finally {
    clearTimeout(timeout);
    dom.connect.disabled = false;
  }
});
dom.disconnect.addEventListener("click", disconnect);

async function capture(epoch, revision, signal) {
  // The renderer enforces a per-run capture budget, including captures after a control.
  await pause(Math.max(0, nextCaptureAt - performance.now()), signal);
  if (signal.aborted) throw new DOMException("Capture cancelled", "AbortError");
  if (!current(epoch) || revision !== frameRevision) return;
  const image = await request("/v1/view/images", { method: "POST", body: {}, signal });
  if (!current(epoch) || revision !== frameRevision) return;
  nextCaptureAt = performance.now() + 1000;
  if (typeof image.image_id !== "string" || !Number.isInteger(image.width) || !Number.isInteger(image.height)
      || image.width < 1 || image.height < 1 || image.width > 16384 || image.height > 16384
      || image.width * image.height > 16 * 1024 * 1024) throw new APIError(502);
  const response = await request(`/v1/images/${encodeURIComponent(image.image_id)}`, { raw: true, signal });
  if (response.headers.get("Content-Type")?.split(";")[0].trim() !== "image/png") throw new APIError(502);
  const maxBytes = 32 * 1024 * 1024;
  if (Number(response.headers.get("Content-Length")) > maxBytes || !response.body) throw new APIError(502);
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > maxBytes) throw new APIError(502);
      chunks.push(chunk.value);
    }
  } finally { await reader.cancel(); }
  const blob = new Blob(chunks, { type: "image/png" });
  const url = URL.createObjectURL(blob);
  try {
    const decoded = new Image();
    decoded.src = url;
    await decoded.decode();
    if (!current(epoch) || revision !== frameRevision || signal.aborted) return;
    if (decoded.naturalWidth !== image.width || decoded.naturalHeight !== image.height) throw new APIError(502);
    const previousURL = frameURL;
    dom["game-image"].src = url;
    frameURL = url;
    frame = { id: image.image_id, width: image.width, height: image.height };
    dom["game-image"].hidden = false;
    dom["view-empty"].hidden = true;
    if (previousURL) URL.revokeObjectURL(previousURL);
    status(dom["view-status"], `Live · ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`, true);
    dom["view-note"].textContent = "Read-only viewer · One shared camera per run; another tab can move it.";
  } finally {
    if (frameURL !== url) URL.revokeObjectURL(url);
  }
}

// The camera has one in-flight operation, including capture, PNG fetch and decode.
async function runView(control = null) {
  if (!session || frameBusy || (control && !frame)) return;
  clearTimeout(frameTimer);
  const epoch = generation, revision = ++frameRevision;
  frameAbort?.abort();
  const controller = new AbortController();
  frameAbort = controller;
  frameBusy = true;
  setViewControls();
  const timeout = setTimeout(() => controller.abort(), 20000);
  let retryDelay = 1000;
  try {
    if (control) await request("/v1/view/controls", {
      method: "POST", body: { image_id: frame.id, ...control }, signal: controller.signal,
    });
    if (current(epoch)) await capture(epoch, revision, controller.signal);
  } catch (error) {
    if (current(epoch)) {
      status(dom["view-status"], stopped(error) ? "View timed out" : "View unavailable");
      dom["view-note"].textContent = "View unavailable. Chat remains connected. Use Refresh to try again.";
      retryDelay = 5000;
      // A new capture or another tab can invalidate the old targeting token.
      frame = null;
    }
  } finally {
    clearTimeout(timeout);
    if (current(epoch) && revision === frameRevision) {
      frameBusy = false;
      setViewControls();
      if (dom["live-view"].checked) frameTimer = setTimeout(() => {
        if (!document.hidden) void runView();
      }, retryDelay);
    }
  }
}

function control(action, parameters = {}) {
  if (frameBusy || !frame) return;
  if (action === "pan") parameters = { dx: Math.max(-frame.width, Math.min(frame.width, parameters.dx)), dy: Math.max(-frame.height, Math.min(frame.height, parameters.dy)) };
  void runView({ action, ...parameters });
}
document.querySelectorAll("[data-pan]").forEach(button => button.addEventListener("click", () => {
  const [dx, dy] = button.dataset.pan.split(",").map(Number);
  control("pan", { dx, dy });
}));
document.querySelectorAll("[data-zoom]").forEach(button => button.addEventListener("click", () => control("zoom", { steps: Number(button.dataset.zoom) })));
dom["refresh-view"].addEventListener("click", () => void runView());
dom["live-view"].addEventListener("change", () => {
  clearTimeout(frameTimer);
  if (dom["live-view"].checked) void runView();
});
dom.inspect.addEventListener("click", () => { inspectMode = !inspectMode; setViewControls(); });
dom["game-image"].addEventListener("click", event => {
  if (!inspectMode || frameBusy || !frame) return;
  // img uses auto dimensions with max-width/max-height, so its rectangle excludes letterboxing.
  const bounds = dom["game-image"].getBoundingClientRect();
  const x = Math.floor((event.clientX - bounds.left) * frame.width / bounds.width);
  const y = Math.floor((event.clientY - bounds.top) * frame.height / bounds.height);
  if (x < 0 || y < 0 || x >= frame.width || y >= frame.height) return;
  control("inspect", { x, y });
});
dom["game-stage"].addEventListener("keydown", event => {
  const keys = { ArrowUp: [0, -160], ArrowDown: [0, 160], ArrowLeft: [-240, 0], ArrowRight: [240, 0] };
  if (keys[event.key]) { event.preventDefault(); const [dx, dy] = keys[event.key]; control("pan", { dx, dy }); }
  else if (["+", "=", "-", "_"].includes(event.key)) { event.preventDefault(); control("zoom", { steps: ["+", "="].includes(event.key) ? -1 : 1 }); }
  else if (event.key === "Enter" && inspectMode && frame) { event.preventDefault(); control("inspect", { x: Math.floor(frame.width / 2), y: Math.floor(frame.height / 2) }); }
});
dom["open-window"].addEventListener("click", () => {
  const [window, vehicle_type] = dom["report-window"].value.split(":");
  control("open_window", { window, ...(vehicle_type ? { vehicle_type } : {}) });
});
dom["close-windows"].addEventListener("click", () => control("close_windows"));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && session && dom["live-view"].checked) void runView();
});

async function refreshParticipants(epoch) {
  try {
    const page = await request("/v1/participants");
    if (!current(epoch)) return;
    const participants = Array.isArray(page.participants) ? page.participants.slice(0, 100) : [];
    dom["participant-count"].textContent = String(participants.length);
    dom["players-status"].textContent = participants.length ? "Humans and agents in this world." : "No players to show yet.";
    const items = participants.map(person => {
      const item = element("li", "participant"), heading = element("div", "participant-heading");
      heading.append(element("span", "participant-name", person.display_name || "Player"), element("span", `badge${person.kind === "agent" ? " agent" : ""}`, person.kind === "agent" ? "AGENT" : "HUMAN"));
      const company = element("div", "company-label");
      company.append(element("span", "company-mark"), element("span", "", companyLabel(person.company_id, person.company_name)));
      item.append(heading, company);
      const presence = person.presence === "online" ? "Online" : person.presence === "offline" ? "Offline" : "Known participant";
      item.append(element("div", "participant-meta", `${presence} · ${String(person.identity_id || "").slice(-6)}`));
      const actions = element("div", "participant-actions");
      if (person.company_id !== undefined && person.company_id !== null && Number(person.company_id) !== 255
          && Number(session.me.company_id) !== 255 && Number(person.company_id) !== Number(session.me.company_id)) {
        const direct = element("button", "quiet", "Message");
        direct.type = "button";
        direct.addEventListener("click", () => void openDirect(person.company_id));
        actions.append(direct);
      }
      if (person.identity_id && person.identity_id !== session.me.identity_id) {
        const block = element("button", "quiet", blocked.has(person.identity_id) ? "Unblock" : "Block");
        block.type = "button";
        block.addEventListener("click", async () => {
          block.disabled = true;
          try {
            const value = !blocked.has(person.identity_id);
            await request("/v1/blocks", { method: "POST", body: { identity_id: person.identity_id, blocked: value } });
            if (!current(epoch)) return;
            if (value) blocked.add(person.identity_id); else blocked.delete(person.identity_id);
            block.textContent = value ? "Unblock" : "Block";
            dom["players-status"].textContent = value ? "Player blocked." : "Player unblocked.";
          } catch { if (current(epoch)) dom["players-status"].textContent = "Could not update block. Try again."; }
          finally { block.disabled = false; }
        });
        actions.append(block);
      }
      item.append(actions);
      return item;
    });
    dom.participants.replaceChildren(...items);
  } catch { if (current(epoch)) dom["players-status"].textContent = "Player list unavailable. Chat remains available."; }
}

function threadName(thread) {
  if (thread.scope === "world") return "Public · Everyone";
  if (thread.scope === "company") return "Company · Your team";
  if (thread.scope === "direct_company") {
    const other = thread.company_ids?.find(id => Number(id) !== Number(session?.me.company_id));
    return `Direct · ${companyLabel(other)}`;
  }
  return "Direct · Player";
}

function refreshThreads(epoch) {
  if (!current(epoch)) return Promise.resolve();
  if (threadRefreshTask) return threadRefreshTask;
  const task = refreshThreadsNow(epoch);
  threadRefreshTask = task;
  void task.finally(() => { if (threadRefreshTask === task) threadRefreshTask = null; });
  return task;
}

async function refreshThreadsNow(epoch) {
  if (!current(epoch) || threadRefreshBusy) return;
  threadRefreshBusy = true;
  try {
    const page = await request("/v1/threads");
    if (!current(epoch)) return;
    threads = Array.isArray(page.threads) ? page.threads.slice(0, 200) : [];
    dom["thread-select"].replaceChildren(...threads.map(thread => {
      const option = element("option", "", threadName(thread) + (thread.muted ? " · muted" : ""));
      option.value = thread.thread_id || thread.id;
      return option;
    }));
    dom["thread-select"].disabled = threads.length === 0;
    if (!threads.some(thread => (thread.thread_id || thread.id) === selectedThread)) {
      await selectThread(threads[0]?.thread_id || threads[0]?.id || null);
    } else {
      dom["thread-select"].value = selectedThread;
      updateThreadUI();
    }
  } catch { if (current(epoch)) status(dom["chat-status"], "Chat unavailable"); }
  finally { if (current(epoch)) threadRefreshBusy = false; }
}

function updateThreadUI() {
  const thread = threads.find(item => (item.thread_id || item.id) === selectedThread);
  const valid = !!session && !!thread;
  dom["message-body"].disabled = !valid;
  dom["send-message"].disabled = !valid;
  dom["mute-thread"].disabled = !valid;
  dom["mute-thread"].textContent = thread?.muted ? "Unmute" : "Mute";
  dom["mute-thread"].setAttribute("aria-pressed", String(!!thread?.muted));
  const descriptions = { world: "Visible to players in this world.", company: "Visible to your company’s players.", direct_company: "Private conversation between these two companies.", direct_identity: "Private conversation between these players." };
  dom["thread-description"].textContent = descriptions[thread?.scope] || "Public, company and direct conversations.";
}

async function selectThread(id) {
  threadRevision++;
  historyAbort?.abort();
  historyTask = null;
  selectedThread = id;
  historyCursor = null;
  historyBusy = false;
  messages = new Map();
  pendingMessage = null;
  dom["message-body"].value = "";
  dom["message-feedback"].textContent = "";
  if (id) dom["thread-select"].value = id;
  updateThreadUI(); renderMessages();
  if (id) await refreshHistory(generation);
}
dom["thread-select"].addEventListener("change", event => void selectThread(event.target.value));

async function openDirect(otherCompany) {
  const epoch = generation;
  try {
    const thread = await request("/v1/threads", { method: "POST", body: { scope: "direct_company", company_ids: [session.me.company_id, otherCompany] } });
    if (!current(epoch)) return;
    await refreshThreads(epoch);
    if (current(epoch)) await selectThread(thread.thread_id || thread.id);
    dom["message-body"].focus();
  } catch { if (current(epoch)) dom["players-status"].textContent = "Could not open conversation. Try again."; }
}

function addMessages(incoming) {
  for (const message of incoming) {
    if (!message || typeof message !== "object") continue;
    const id = message.message_id || message.id;
    if (typeof id === "string" && message.thread_id === selectedThread) messages.set(id, message);
  }
  // Timestamp is presentation ordering only; server cursors remain the source of history progress.
  const ordered = [...messages.entries()].sort((a, b) => String(a[1].timestamp).localeCompare(String(b[1].timestamp)) || a[0].localeCompare(b[0]));
  messages = new Map(ordered.slice(-MAX_MESSAGES));
  renderMessages();
}

function renderMessages() {
  const log = dom.messages;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 70;
  const previousTop = log.scrollTop;
  if (!messages.size) {
    const text = selectedThread ? "Start the conversation." : "Build connections while your trains run.";
    if (log.children.length !== 1 || log.firstElementChild.textContent !== text) log.replaceChildren(element("p", "chat-empty", text));
    return;
  }
  const existing = new Map([...log.children].map(node => [node.dataset.messageId, node]));
  const items = [...messages.values()].map(message => {
    const id = message.message_id || message.id;
    if (existing.has(id)) return existing.get(id);
    const own = message.sender?.identity_id === session?.me.identity_id;
    const item = element("article", `message${own ? " own" : ""}`);
    item.dataset.messageId = id;
    const heading = element("div", "message-heading");
    heading.append(element("span", "message-author", message.sender?.display_name || "Player"));
    heading.append(element("span", `badge${message.sender?.kind === "agent" ? " agent" : ""}`, message.sender?.kind === "agent" ? "AGENT" : "HUMAN"));
    const date = new Date(message.timestamp);
    const time = element("time", "message-time", Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    if (!Number.isNaN(date.getTime())) { time.dateTime = date.toISOString(); time.title = date.toLocaleString(); }
    heading.append(time);
    item.append(heading, element("p", "message-body", typeof message.body === "string" ? message.body : ""));
    if (!own) {
      const report = element("button", "message-report", "Report");
      report.type = "button";
      report.addEventListener("click", () => {
        reportMessage = message.message_id || message.id;
        dom["report-reason"].value = "";
        dom["report-feedback"].textContent = "";
        dom["report-dialog"].showModal();
      });
      item.append(report);
    }
    return item;
  });
  const retained = new Set(items);
  for (const child of [...log.children]) if (!retained.has(child)) child.remove();
  for (let index = 0; index < items.length; index++) {
    if (log.children[index] !== items[index]) log.insertBefore(items[index], log.children[index] || null);
  }
  if (atBottom) log.scrollTop = log.scrollHeight; else log.scrollTop = previousTop;
}

function refreshHistory(epoch) {
  if (!current(epoch)) return Promise.resolve();
  if (historyTask) return historyTask;
  const task = refreshHistoryNow(epoch);
  historyTask = task;
  void task.finally(() => { if (historyTask === task) historyTask = null; });
  return task;
}

async function refreshHistoryNow(epoch) {
  if (!current(epoch) || !selectedThread || historyBusy) return;
  const revision = threadRevision, thread = selectedThread;
  const controller = new AbortController();
  historyAbort = controller; historyBusy = true;
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    // Fetch bounded pages, then resume from the retained server cursor on the next refresh.
    for (let pageNumber = 0; pageNumber < 5; pageNumber++) {
      const query = historyCursor ? `?limit=100&after=${encodeURIComponent(historyCursor)}` : "?limit=100";
      const page = await request(`/v1/threads/${encodeURIComponent(thread)}/messages${query}`, { signal: controller.signal });
      if (!current(epoch) || revision !== threadRevision) return;
      const incoming = Array.isArray(page.messages) ? page.messages : [];
      addMessages(incoming);
      const next = typeof page.next_after === "string" ? page.next_after : historyCursor;
      if (next === historyCursor || incoming.length < 100) { historyCursor = next; break; }
      historyCursor = next;
    }
  } catch (error) {
    if (!stopped(error) && current(epoch) && revision === threadRevision) dom["message-feedback"].textContent = "History unavailable. Retrying; live chat is still separate.";
  } finally {
    clearTimeout(timeout);
    if (current(epoch) && revision === threadRevision) historyBusy = false;
  }
}

async function reconcileChat(epoch) {
  // Finish work that may predate the snapshot, then fetch again after its
  // cursor anchor. History has its own cursor: retain it even across >5 pages.
  await threadRefreshTask;
  if (!current(epoch)) return;
  await refreshThreads(epoch);
  await historyTask;
  if (current(epoch)) await refreshHistory(epoch);
}

function scheduleChat(epoch) {
  clearTimeout(chatTimer);
  chatTimer = setTimeout(async () => {
    if (!current(epoch)) return;
    await Promise.allSettled([refreshThreads(epoch), refreshHistory(epoch), refreshParticipants(epoch)]);
    if (current(epoch)) scheduleChat(epoch);
  }, 5000);
}

dom["message-form"].addEventListener("submit", async event => {
  event.preventDefault();
  const body = dom["message-body"].value.trim();
  if (!session || !selectedThread || !body || dom["send-message"].disabled) return;
  if (new TextEncoder().encode(body).length > 4096) {
    dom["message-feedback"].textContent = "Message is too long. Use at most 4,096 UTF-8 bytes.";
    return;
  }
  const epoch = generation, revision = threadRevision, thread = selectedThread;
  // Reusing the ID for an unchanged failed send makes a retry safe after a lost response.
  if (!pendingMessage || pendingMessage.body !== body || pendingMessage.thread !== thread) pendingMessage = { body, thread, id: requestID() };
  const submitted = pendingMessage;
  dom["send-message"].disabled = true;
  dom["message-feedback"].textContent = "Sending…";
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const message = await request(`/v1/threads/${encodeURIComponent(thread)}/messages`, {
      method: "POST", body: { body, request_id: submitted.id }, signal: controller.signal,
    });
    if (!current(epoch) || revision !== threadRevision) return;
    addMessages([message]);
    if (dom["message-body"].value.trim() === body) dom["message-body"].value = "";
    dom["message-feedback"].textContent = "";
    pendingMessage = null;
  } catch {
    if (current(epoch) && revision === threadRevision) dom["message-feedback"].textContent = "Delivery could not be confirmed. Send again to safely retry.";
  } finally {
    clearTimeout(timeout);
    if (current(epoch) && revision === threadRevision) dom["send-message"].disabled = false;
  }
});
dom["message-body"].addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); dom["message-form"].requestSubmit(); }
});

dom["mute-thread"].addEventListener("click", async () => {
  const epoch = generation, thread = threads.find(item => (item.thread_id || item.id) === selectedThread);
  if (!thread) return;
  dom["mute-thread"].disabled = true;
  try {
    await request("/v1/mutes", { method: "POST", body: { thread_id: selectedThread, muted: !thread.muted } });
    if (!current(epoch)) return;
    thread.muted = !thread.muted;
    updateThreadUI();
  } catch { if (current(epoch)) dom["message-feedback"].textContent = "Could not update mute. Try again."; }
  finally { if (current(epoch)) updateThreadUI(); }
});
dom["cancel-report"].addEventListener("click", () => dom["report-dialog"].close());
dom["report-form"].addEventListener("submit", async event => {
  event.preventDefault();
  if (!reportMessage || !session) return;
  const reason = dom["report-reason"].value.trim(), epoch = generation;
  if (!reason) return;
  const button = dom["report-form"].querySelector("[type=submit]");
  button.disabled = true;
  try {
    await request("/v1/reports", { method: "POST", body: { message_id: reportMessage, reason } });
    if (current(epoch)) { dom["report-dialog"].close(); dom["message-feedback"].textContent = "Report sent to the world operator."; }
  } catch { if (current(epoch)) dom["report-feedback"].textContent = "Could not send report. Try again."; }
  finally { button.disabled = false; }
});

function pause(milliseconds, signal) {
  return new Promise(resolve => {
    if (signal.aborted) return resolve();
    const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
    const timer = setTimeout(done, milliseconds);
    signal.addEventListener("abort", done, { once: true });
  });
}

async function connectEvents(epoch) {
  eventAbort?.abort();
  const controller = new AbortController();
  eventAbort = controller;
  let backoff = 1000;
  let refreshCursor = false;
  while (current(epoch) && !controller.signal.aborted) {
    let reader;
    const attempt = new AbortController();
    const abort = () => attempt.abort();
    controller.signal.addEventListener("abort", abort, { once: true });
    try {
      status(dom["chat-status"], "Connecting…");
      showWorldStatus();
      const snapshotController = new AbortController();
      const abortSnapshot = () => snapshotController.abort();
      attempt.signal.addEventListener("abort", abortSnapshot, { once: true });
      const timeout = setTimeout(abortSnapshot, 15000);
      let snapshot;
      try {
        snapshot = await request("/v1/world/status", { signal: snapshotController.signal, decode: text => parseWorldStatus(text, true) });
        if (worldStatus && sameWorldFence(worldStatus, snapshot) && snapshot.pause.revision < worldStatus.pause.revision) throw new TypeError("Stale world snapshot");
      } catch {
        // The native world can be recovering while backend chat still works.
        // Never promote cached events to current state after a failed GET.
        snapshot = null;
        if (current(epoch) && !controller.signal.aborted) showWorldStatus();
      } finally {
        clearTimeout(timeout);
        attempt.signal.removeEventListener("abort", abortSnapshot);
      }
      if (!current(epoch) || controller.signal.aborted) return;
      if (snapshot) {
        const nextCursor = statusStreamCursor(worldStatus, snapshot, cursor, refreshCursor);
        const reset = refreshCursor || !cursor || !sameWorldFence(worldStatus, snapshot);
        worldStatus = snapshot;
        showWorldStatus(snapshot);
        if (reset) await reconcileChat(epoch);
        if (!current(epoch) || controller.signal.aborted) return;
        cursor = nextCursor;
        refreshCursor = false;
      }
      if (!current(epoch) || controller.signal.aborted) return;
      const response = await fetch("/v1/events", {
        headers: { Authorization: `Bearer ${session.token}`, Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) },
        credentials: "omit", cache: "no-store", redirect: "error", signal: attempt.signal,
      });
      if ([400, 409, 410].includes(response.status) && cursor) {
        cursor = "";
        refreshCursor = true;
        throw new APIError(response.status);
      }
      if (!response.ok || !response.body || !response.headers.get("Content-Type")?.startsWith("text/event-stream")) throw new APIError(response.status);
      if (!current(epoch)) return;
      status(dom["chat-status"], "Live", true);
      backoff = 1000;
      const parser = createSSEParser(event => {
        if (!current(epoch)) return;
        if (event.id.length > 128) throw new RangeError("Invalid event cursor");
        // One durable cursor carries both chat and shared status. Events are
        // data, never instructions or HTML.
        cursor = event.id;
        if (event.type === "world.status") {
          const incoming = parseWorldStatus(event.data);
          const decision = snapshot ? worldStatusDecision(worldStatus, incoming) : "refresh";
          if (decision === "refresh") {
            refreshCursor = true;
            throw new Error("Fresh world status required");
          }
          if (decision === "apply") { worldStatus = incoming; showWorldStatus(incoming); }
          return;
        }
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        if (!data || typeof data !== "object" || Array.isArray(data)) return;
        if (event.type === "chat.message") {
          addMessages([data]);
          if (!threads.some(thread => (thread.thread_id || thread.id) === data.thread_id)) void refreshThreads(epoch);
        } else if (event.type === "chat.message.tick") {
          const message = messages.get(data.message_id);
          if (message) { message.tick = data.tick; message.tick_authoritative = data.tick_authoritative; }
        }
      });
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      while (current(epoch) && !controller.signal.aborted) {
        const chunk = await reader.read();
        if (chunk.done) { parser.push(decoder.decode()); parser.finish(); break; }
        parser.push(decoder.decode(chunk.value, { stream: true }));
      }
    } catch (error) {
      if (!current(epoch) || controller.signal.aborted) return;
      status(dom["chat-status"], "Reconnecting…");
      showWorldStatus();
    } finally {
      controller.signal.removeEventListener("abort", abort);
      if (reader) { try { await reader.cancel(); } catch { /* Closed transport. */ } }
    }
    if (!current(epoch) || controller.signal.aborted) return;
    status(dom["chat-status"], "Reconnecting…");
    showWorldStatus();
    await pause(backoff + Math.floor(Math.random() * 250), controller.signal);
    backoff = Math.min(backoff * 2, 30000);
  }
}

window.addEventListener("pagehide", disconnect);
setViewControls();
showWorldStatus();
