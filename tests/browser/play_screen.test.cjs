const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const AiRpgPending = require("../../src/ai_rpg/api/static/play-state.js");
const html = fs.readFileSync("src/ai_rpg/api/static/index.html", "utf8");
const inlineScript = [...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)].at(-1)[1];

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class FakeNode {}

class FakeElement extends FakeNode {
  constructor(id = "") {
    super();
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.disabled = false;
    this.hidden = false;
    this.parent = null;
  }

  addEventListener(type, listener) { this.listeners[type] = listener; }
  setAttribute(name, value) { this[name] = value; }
  async dispatch(type, event = {}) { return this.listeners[type]?.(event); }
  append(...children) {
    for (const child of children) {
      if (child instanceof FakeElement) child.parent = this;
      this.children.push(child);
    }
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
  }
  scrollIntoView() {}
  focus() {}
  requestSubmit() { return this.dispatch("submit", { preventDefault() {} }); }
  querySelector(selector) {
    if (selector === ".message-body") return find(this, node => node.className === "message-body");
    if (selector === ".pending span:last-child") {
      const pending = find(this, node => node.className === "notice pending");
      return pending?.children.at(-1) ?? null;
    }
    return null;
  }
  get lastElementChild() { return this.children.filter(child => child instanceof FakeElement).at(-1) ?? null; }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) {
    if (child instanceof FakeElement) {
      const match = find(child, predicate);
      if (match) return match;
    }
  }
  return null;
}

function terminalTurn(turnId, version) {
  return {
    turn_id: turnId,
    route: "narrative",
    resolution_status: "committed",
    narration_status: "completed",
    committed_state_version: version,
    narration: "続行した。",
    choices: [],
    action_results: [],
    recovery: { fallback: false, reason: null },
  };
}

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, async json() { return body; } };
}

function pendingTurn(turnId) {
  return {
    ...terminalTurn(turnId, 0),
    resolution_status: "resolving",
    narration_status: "generating",
    narration: null,
  };
}

function eventSourceTracker() {
  const instances = [];
  class EventSource {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.closed = 0;
      instances.push(this);
    }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    close() { this.closed += 1; }
    emit(type, turn) {
      if (type === "open") return this.onopen?.();
      if (type === "error") return this.onerror?.();
      return this.listeners[type]?.({ data: JSON.stringify({ payload: { turn } }) });
    }
  }
  return { EventSource, instances };
}

function timerTracker() {
  const callbacks = new Map();
  let nextId = 0;
  return {
    setTimeout(callback) {
      const id = ++nextId;
      callbacks.set(id, callback);
      return id;
    },
    clearTimeout(id) { callbacks.delete(id); },
    async runAll() {
      for (const [id, callback] of [...callbacks]) {
        callbacks.delete(id);
        await callback();
      }
    },
  };
}

function harness(fetch, ids = ["request-1"], initialPending = null, options = {}) {
  const {
    EventSource,
    setTimeout: schedule = setTimeout,
    clearTimeout: cancel = clearTimeout,
  } = options;
  const elementIds = [
    "campaign-id", "actor-id", "action-text", "send-action", "composer", "timeline",
    "empty-state", "connection", "connection-label", "route-value", "resolution-value",
    "narration-value", "version-value", "turn-value", "recovery", "live-status",
  ];
  const elements = Object.fromEntries(elementIds.map(id => [id, new FakeElement(id)]));
  elements.timeline.append(elements["empty-state"]);
  const document = {
    getElementById(id) { return elements[id] ?? null; },
    createElement() { return new FakeElement(); },
    querySelectorAll() { return []; },
    querySelector(selector) {
      if (selector === '[data-role="processing"]') {
        return find(elements.timeline, node => node.dataset.role === "processing");
      }
      if (selector === '[data-role="processing"] .pending span:last-child') {
        return document.querySelector('[data-role="processing"]')?.querySelector(".pending span:last-child") ?? null;
      }
      return null;
    },
  };
  const storage = new MemoryStorage();
  if (initialPending) AiRpgPending.save(storage, initialPending);
  const uuidValues = [...ids];
  const context = vm.createContext({
    AiRpgPending,
    Node: FakeNode,
    document,
    localStorage: storage,
    crypto: { randomUUID() { return uuidValues.shift(); } },
    fetch,
    window: EventSource ? { EventSource } : {},
    EventSource,
    console,
    setTimeout: schedule,
    clearTimeout: cancel,
    Error,
  });
  new vm.Script(inlineScript).runInContext(context);
  return { context, elements, storage };
}

async function flush() {
  for (let index = 0; index < 8; index += 1) await new Promise(setImmediate);
}

test("response loss retries the exact same request id and body", async () => {
  const posts = [];
  let postCount = 0;
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      postCount += 1;
      if (postCount === 1) throw new TypeError("response lost");
      return response(terminalTurn("turn-1", 0), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  });
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "調べる";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  const retry = find(app.elements.timeline, node => node.textContent === "同じ送信を再試行");
  assert.ok(retry);
  await retry.dispatch("click");
  await flush();

  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1], posts[0]);
  assert.equal(posts[0].request_id, "request-1");
  assert.equal(AiRpgPending.load(app.storage), null);
});

test("5xx retries the exact same request id and body", async () => {
  const posts = [];
  let postCount = 0;
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      postCount += 1;
      if (postCount === 1) return response({ detail: { code: "UNKNOWN" } }, 503);
      return response(terminalTurn("turn-1", 0), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  });
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "調べる";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  assert.deepEqual(AiRpgPending.load(app.storage).body, posts[0]);

  const retry = find(app.elements.timeline, node => node.textContent === "同じ送信を再試行");
  assert.ok(retry);
  await retry.dispatch("click");
  await flush();

  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1], posts[0]);
  assert.equal(posts[0].request_id, "request-1");
  assert.equal(AiRpgPending.load(app.storage), null);
});

test("successful POST JSON parse failure retries the exact same request id and body", async () => {
  const posts = [];
  let postCount = 0;
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      postCount += 1;
      if (postCount === 1) {
        return { ok: true, status: 202, async json() { throw new SyntaxError("invalid JSON"); } };
      }
      return response(terminalTurn("turn-1", 0), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  });
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "調べる";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  assert.deepEqual(AiRpgPending.load(app.storage).body, posts[0]);

  const retry = find(app.elements.timeline, node => node.textContent === "同じ送信を再試行");
  assert.ok(retry);
  await retry.dispatch("click");
  await flush();

  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1], posts[0]);
  assert.equal(posts[0].request_id, "request-1");
  assert.equal(AiRpgPending.load(app.storage), null);
});

test("422 clears pending and corrected input uses a new request id", async () => {
  const posts = [];
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      if (posts.length === 1) return response({ detail: { code: "UNKNOWN" } }, 422);
      return response(terminalTurn("turn-2", 0), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-1", "request-2"]);
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  assert.equal(AiRpgPending.load(app.storage), null);
  assert.equal(
    find(app.elements.timeline, node => node.textContent === "同じ送信を再試行"),
    null,
  );
  assert.ok(find(
    app.elements.timeline,
    node => node.className === "notice error" && node.textContent === "API error (422)",
  ));

  app.elements["action-text"].value = "引き返す";
  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();

  assert.equal(posts.length, 2);
  assert.equal(posts[1].request_id, "request-2");
  assert.equal(posts[1].content.text, "引き返す");
});

test("latest turn version never overwrites current campaign version", async () => {
  const posts = [];
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) {
      return response({ state_version: 2, latest_turn: terminalTurn("turn-before", 1) });
    }
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      return response(terminalTurn("turn-2", 2), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-2"]);
  app.elements["campaign-id"].value = "campaign-a";
  await app.elements["campaign-id"].dispatch("change");
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();

  assert.equal(posts.length, 1);
  assert.equal(posts[0].expected_state_version, 2);
  assert.equal(app.elements["version-value"].textContent, "2");
});

test("delayed same-campaign state response cannot resume an obsolete turn", async () => {
  let stateCalls = 0;
  let resolveFirstState;
  const firstState = new Promise(resolve => { resolveFirstState = resolve; });
  const currentTurn = { ...terminalTurn("turn-current", 2), narration: "current" };
  const obsoleteTurn = { ...terminalTurn("turn-obsolete", 1), narration: "obsolete" };
  const app = harness(async path => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) {
      stateCalls += 1;
      if (stateCalls === 1) return firstState;
      return response({ state_version: 2, latest_turn: currentTurn });
    }
    throw new Error(`unexpected request: ${path}`);
  });
  app.elements["campaign-id"].value = "campaign-a";

  const olderRefresh = app.context.refreshCampaignState("campaign-a", { resume: true });
  await flush();
  const newerRefresh = app.context.refreshCampaignState("campaign-a", { resume: true });
  await newerRefresh;
  resolveFirstState(response({ state_version: 1, latest_turn: obsoleteTurn }));
  await olderRefresh;

  assert.equal(app.elements["version-value"].textContent, "2");
  assert.equal(app.elements["turn-value"].textContent, "turn-current");
  assert.equal(find(app.elements.timeline, node => node.textContent === "obsolete"), null);
});

test("lower state version response is ignored before resuming its turn", async () => {
  const states = [
    { state_version: 2, latest_turn: terminalTurn("turn-current", 2) },
    { state_version: 1, latest_turn: terminalTurn("turn-obsolete", 1) },
  ];
  const app = harness(async path => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response(states.shift());
    throw new Error(`unexpected request: ${path}`);
  });
  app.elements["campaign-id"].value = "campaign-a";

  await app.context.refreshCampaignState("campaign-a", { resume: true });
  await app.context.refreshCampaignState("campaign-a", { resume: true });

  assert.equal(app.elements["version-value"].textContent, "2");
  assert.equal(app.elements["turn-value"].textContent, "turn-current");
});

test("state conflict refreshes version without replaying the rejected action", async () => {
  const posts = [];
  let stateVersion = 2;
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: stateVersion, latest_turn: null });
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      if (posts.length === 1) {
        stateVersion = 3;
        return response({ detail: { code: "STATE_VERSION_CONFLICT" } }, 409);
      }
      return response(terminalTurn("turn-2", 3), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-1", "request-2"]);
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();

  assert.equal(posts.length, 1);
  assert.equal(AiRpgPending.load(app.storage), null);
  assert.equal(app.elements["version-value"].textContent, "3");

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();

  assert.equal(posts.length, 2);
  assert.equal(posts[1].expected_state_version, 3);
  assert.equal(posts[1].request_id, "request-2");
});

test("campaign switch reads its own version and stale action is not auto-submitted", async () => {
  const posts = [];
  let campaignBVersion = 0;
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path === "/campaigns/campaign-a/state") {
      return response({ state_version: 9, latest_turn: null });
    }
    if (path === "/campaigns/campaign-b/state") {
      return response({ state_version: campaignBVersion, latest_turn: null });
    }
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      return response(terminalTurn("turn-b", campaignBVersion), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-b"]);

  app.elements["campaign-id"].value = "campaign-a";
  await app.elements["campaign-id"].dispatch("change");
  app.elements["campaign-id"].value = "campaign-b";
  await app.elements["campaign-id"].dispatch("change");
  app.elements["actor-id"].value = "actor-b";
  app.elements["action-text"].value = "進む";
  campaignBVersion = 1;

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  assert.equal(posts.length, 0);
  assert.equal(app.elements["version-value"].textContent, "1");

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].expected_state_version, 1);
  assert.equal(posts[0].request_id, "request-b");
});

test("stale successful restore refresh does not continue old pending work", async () => {
  const calls = [];
  let resolveOldState;
  const oldState = new Promise(resolve => { resolveOldState = resolve; });
  const pending = AiRpgPending.create({
    campaignId: "campaign-a",
    actorId: "actor-a",
    displayText: "調べる",
    content: { kind: "text", text: "調べる" },
    stateVersion: 3,
    requestId: "request-old",
  });
  const app = harness(async (path, options = {}) => {
    calls.push([path, options.method ?? "GET"]);
    if (path === "/health") return response({ status: "ok" });
    if (path === "/campaigns/campaign-a/state") return oldState;
    if (path === "/campaigns/campaign-b/state") {
      return response({ state_version: 4, latest_turn: null });
    }
    if (options.method === "POST") return response(terminalTurn("turn-old", 3), 202);
    throw new Error(`unexpected request: ${path}`);
  }, [], pending);
  await flush();

  app.elements["campaign-id"].value = "campaign-b";
  await app.elements["campaign-id"].dispatch("change");
  resolveOldState(response({ state_version: 3, latest_turn: null }));
  await flush();

  assert.equal(calls.filter(([, method]) => method === "POST").length, 0);
  assert.equal(AiRpgPending.load(app.storage).body.request_id, "request-old");
  assert.equal(app.elements["version-value"].textContent, "4");
});

test("stale failed restore refresh does not show an old campaign error", async () => {
  let rejectOldState;
  const oldState = new Promise((_, reject) => { rejectOldState = reject; });
  const pending = AiRpgPending.create({
    campaignId: "campaign-a",
    actorId: "actor-a",
    displayText: "調べる",
    content: { kind: "text", text: "調べる" },
    stateVersion: 3,
    requestId: "request-old",
  });
  const app = harness(async path => {
    if (path === "/health") return response({ status: "ok" });
    if (path === "/campaigns/campaign-a/state") return oldState;
    if (path === "/campaigns/campaign-b/state") {
      return response({ state_version: 4, latest_turn: null });
    }
    throw new Error(`unexpected request: ${path}`);
  }, [], pending);
  await flush();

  app.elements["campaign-id"].value = "campaign-b";
  await app.elements["campaign-id"].dispatch("change");
  rejectOldState(new Error("Campaign A refresh failed"));
  await flush();

  assert.equal(
    find(app.elements.timeline, node => node.textContent === "Campaign A refresh failed"),
    null,
  );
  assert.equal(app.elements["version-value"].textContent, "4");
});

test("reload tracks an accepted turn by GET without another POST", async () => {
  const calls = [];
  const pending = {
    ...AiRpgPending.create({
      campaignId: "campaign-a",
      actorId: "actor-a",
      displayText: "調べる",
      content: { kind: "text", text: "調べる" },
      stateVersion: 3,
      requestId: "request-known",
    }),
    turnId: "turn-known",
  };
  const app = harness(async (path, options = {}) => {
    calls.push([path, options.method ?? "GET"]);
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 3, latest_turn: null });
    if (path.endsWith("/turns/turn-known")) return response(terminalTurn("turn-known", 3));
    throw new Error(`unexpected request: ${path}`);
  }, [], pending);

  await flush();

  assert.ok(calls.some(([path]) => path.endsWith("/turns/turn-known")));
  assert.equal(calls.filter(([, method]) => method === "POST").length, 0);
  assert.equal(AiRpgPending.load(app.storage), null);
});

test("tracking failure preserves the accepted turn id and exact request body", async () => {
  const posts = [];
  let turnGets = 0;
  const events = eventSourceTracker();
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 3, latest_turn: null });
    if (options.method === "POST") {
      posts.push(JSON.parse(options.body));
      return response(pendingTurn("turn-known"), 202);
    }
    if (path.endsWith("/turns/turn-known")) {
      turnGets += 1;
      if (turnGets === 1) throw new Error("tracking failed");
      return response(terminalTurn("turn-known", 3));
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-known"], null, events);
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "調べる";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  events.instances[0].emit("error");
  await flush();

  const saved = AiRpgPending.load(app.storage);
  assert.equal(saved.turnId, "turn-known");
  assert.deepEqual(saved.body, posts[0]);
  assert.equal(saved.body.request_id, "request-known");

  const retry = find(app.elements.timeline, node => node.textContent === "同じ送信を再試行");
  assert.ok(retry);
  await retry.dispatch("click");
  await flush();

  assert.equal(posts.length, 1);
  assert.equal(turnGets, 2);
  assert.equal(AiRpgPending.load(app.storage), null);
});

test("healthy SSE does not start polling", async () => {
  const calls = [];
  const events = eventSourceTracker();
  const timers = timerTracker();
  const app = harness(async (path, options = {}) => {
    calls.push([path, options.method ?? "GET"]);
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") return response(pendingTurn("turn-1"), 202);
    if (path.endsWith("/turns/turn-1")) return response(terminalTurn("turn-1", 0));
    throw new Error(`unexpected request: ${path}`);
  }, ["request-1"], null, { ...events, ...timers });
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  events.instances[0].emit("open");
  await timers.runAll();
  await flush();

  assert.equal(calls.filter(([path]) => path.endsWith("/turns/turn-1")).length, 0);
});

test("poll failure closes SSE and stale events cannot update status", async () => {
  const events = eventSourceTracker();
  const timers = timerTracker();
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") return response(pendingTurn("turn-1"), 202);
    if (path.endsWith("/turns/turn-1")) throw new Error("poll failed");
    throw new Error(`unexpected request: ${path}`);
  }, ["request-1"], null, { ...events, ...timers });
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  events.instances[0].emit("error");
  await flush();
  assert.equal(events.instances[0].closed, 1);

  app.elements["turn-value"].textContent = "newer-turn";
  events.instances[0].emit("turn.updated", pendingTurn("turn-1"));
  await flush();

  assert.equal(app.elements["turn-value"].textContent, "newer-turn");
});

test("late polling response from an old generation is ignored", async () => {
  const events = eventSourceTracker();
  let resolveOldPoll;
  const oldPoll = new Promise(resolve => { resolveOldPoll = resolve; });
  const app = harness(async path => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/turns/turn-old")) return oldPoll;
    throw new Error(`unexpected request: ${path}`);
  }, [], null, events);
  app.elements["campaign-id"].value = "campaign-a";

  const oldTracking = app.context.waitForTurn("campaign-a", "turn-old");
  events.instances[0].emit("error");
  await flush();
  const newTracking = app.context.waitForTurn("campaign-a", "turn-new");
  events.instances[1].emit("turn.updated", terminalTurn("turn-new", 0));
  await newTracking;

  resolveOldPoll(response(terminalTurn("turn-old", 0)));
  await oldTracking;
  await flush();
  assert.equal(app.elements["turn-value"].textContent, "turn-new");
});

test("replacement resume keeps the new workflow busy", async () => {
  const events = eventSourceTracker();
  const app = harness(async path => {
    if (path === "/health") return response({ status: "ok" });
    throw new Error(`unexpected request: ${path}`);
  }, [], null, events);
  app.elements["campaign-id"].value = "campaign-a";

  const oldWorkflow = app.context.resumeTurn("campaign-a", pendingTurn("turn-old"));
  await flush();
  const newWorkflow = app.context.resumeTurn("campaign-a", pendingTurn("turn-new"));
  await flush();
  await oldWorkflow;

  assert.equal(app.elements["send-action"].disabled, true);
  assert.equal(find(app.elements.timeline, node => node.className === "notice error"), null);

  events.instances[1].emit("turn.updated", terminalTurn("turn-new", 0));
  await newWorkflow;
  assert.equal(app.elements["send-action"].disabled, false);
});

test("replacement pending workflow keeps the newer pending operation", async () => {
  const events = eventSourceTracker();
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      return response(pendingTurn(body.content.text === "old" ? "turn-old" : "turn-new"), 202);
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-old", "request-new"], null, events);
  app.elements["campaign-id"].value = "campaign-a";
  const oldPending = AiRpgPending.create({
    campaignId: "campaign-a", actorId: "actor-a", displayText: "old",
    content: { kind: "text", text: "old" }, stateVersion: 0, requestId: "request-old",
  });
  const newPending = AiRpgPending.create({
    campaignId: "campaign-a", actorId: "actor-a", displayText: "new",
    content: { kind: "text", text: "new" }, stateVersion: 0, requestId: "request-new",
  });

  const oldWorkflow = app.context.continuePending(oldPending, false);
  await flush();
  const newWorkflow = app.context.continuePending(newPending, false);
  await flush();
  await oldWorkflow;

  assert.equal(AiRpgPending.load(app.storage).turnId, "turn-new");
  assert.equal(app.elements["send-action"].disabled, true);
  assert.equal(find(app.elements.timeline, node => node.className === "notice error"), null);

  events.instances[1].emit("turn.updated", terminalTurn("turn-new", 0));
  await newWorkflow;
  assert.equal(AiRpgPending.load(app.storage), null);
});

test("campaign switch makes stale SSE callbacks inert", async () => {
  const events = eventSourceTracker();
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") return response(pendingTurn("turn-old"), 202);
    throw new Error(`unexpected request: ${path}`);
  }, ["request-old"], null, events);
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  const pendingText = find(app.elements.timeline, node => node.className === "notice pending").children.at(-1);
  app.elements["campaign-id"].value = "campaign-b";
  app.elements["connection-label"].textContent = "Campaign B";
  pendingText.textContent = "Campaign B pending";
  events.instances[0].emit("turn.updated", pendingTurn("turn-old"));
  events.instances[0].emit("open");
  await flush();

  assert.equal(pendingText.textContent, "Campaign B pending");
  assert.equal(app.elements["connection-label"].textContent, "Campaign B");
});

test("campaign switch blocks stale fallback UI", async () => {
  const events = eventSourceTracker();
  const timers = timerTracker();
  let turnGets = 0;
  const app = harness(async (path, options = {}) => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: null });
    if (options.method === "POST") return response(pendingTurn("turn-old"), 202);
    if (path.endsWith("/turns/turn-old")) {
      turnGets += 1;
      return response(terminalTurn("turn-old", 0));
    }
    throw new Error(`unexpected request: ${path}`);
  }, ["request-old"], null, { ...events, ...timers });
  app.elements["campaign-id"].value = "campaign-a";
  app.elements["actor-id"].value = "actor-a";
  app.elements["action-text"].value = "進む";

  await app.elements.composer.dispatch("submit", { preventDefault() {} });
  await flush();
  const pendingText = find(app.elements.timeline, node => node.className === "notice pending").children.at(-1);
  app.elements["campaign-id"].value = "campaign-b";
  app.elements["connection-label"].textContent = "Campaign B";
  pendingText.textContent = "Campaign B pending";
  await timers.runAll();
  await flush();

  assert.equal(app.elements["connection-label"].textContent, "Campaign B");
  assert.equal(turnGets, 0);
  assert.equal(pendingText.textContent, "Campaign B pending");
  assert.equal(app.elements["connection-label"].textContent, "Campaign B");
});

test("campaign switch ignores a delayed polling response", async () => {
  const events = eventSourceTracker();
  const timers = timerTracker();
  let resolveOldPoll;
  const oldPoll = new Promise(resolve => { resolveOldPoll = resolve; });
  const app = harness(async path => {
    if (path === "/health") return response({ status: "ok" });
    if (path.endsWith("/turns/turn-old")) return oldPoll;
    throw new Error(`unexpected request: ${path}`);
  }, [], null, { ...events, ...timers });
  app.elements["campaign-id"].value = "campaign-a";
  app.context.processingMessage();
  const tracking = app.context.waitForTurn("campaign-a", "turn-old");
  await timers.runAll();
  await flush();
  const pendingText = find(app.elements.timeline, node => node.className === "notice pending").children.at(-1);
  app.elements["campaign-id"].value = "campaign-b";
  app.elements["connection-label"].textContent = "Campaign B";
  pendingText.textContent = "Campaign B pending";

  resolveOldPoll(response(terminalTurn("turn-old", 0)));
  await flush();
  await tracking;

  assert.equal(pendingText.textContent, "Campaign B pending");
  assert.equal(app.elements["connection-label"].textContent, "Campaign B");
});
