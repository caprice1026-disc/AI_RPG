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
      if (child instanceof FakeElement) { child.remove(); child.parent = this; }
      this.children.push(child);
    }
  }
  prepend(...children) {
    for (const child of [...children].reverse()) {
      child.remove(); child.parent = this; this.children.unshift(child);
    }
  }
  before(...children) {
    if (!this.parent) return;
    for (const child of children) {
      child.remove();
      child.parent = this.parent;
      this.parent.children.splice(this.parent.children.indexOf(this), 0, child);
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
  const elementIds = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = Object.fromEntries(elementIds.map(id => [id, new FakeElement(id)]));
  elements.timeline.append(elements["empty-state"]);
  const document = {
    getElementById(id) { return id === "empty-state" ? find(elements.timeline, node => node.id === id) : elements[id] ?? null; },
    createElement() { return new FakeElement(); },
    querySelectorAll(selector) {
      const found = new Set();
      const visit = node => {
        if (selector === ".choice" && node.className === "choice") found.add(node);
        for (const child of node.children || []) visit(child);
      };
      Object.values(elements).forEach(visit);
      return [...found];
    },
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
  const storage = options.storage || new MemoryStorage();
  if (initialPending) AiRpgPending.save(storage, initialPending);
  const uuidValues = [...ids];
  const context = vm.createContext({
    AiRpgPending,
    Node: FakeNode,
    document,
    localStorage: storage,
    crypto: { randomUUID() { return uuidValues.shift(); } },
    fetch: (path, request = {}) => {
      // Legacy recovery fixtures have no adventure catalog/history. New UI tests
      // opt in to the full API so missing or incorrect requests fail explicitly.
      if (!options.adventureApi) {
        if (path === "/adventures/catalog") return Promise.resolve(response({ scenarios: [], presets: [] }));
        if (path === "/adventures" && !request.method) return Promise.resolve(response({ adventures: [] }));
        if (path.includes("/history?")) return Promise.resolve(response({ items: [], next_before_turn_id: null }));
      }
      return fetch(path, request);
    },
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
    if (path.endsWith("/state")) return response({ state_version: 0, latest_turn: terminalTurn("turn-new", 0) });
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

  AiRpgPending.save(app.storage, oldPending);
  const oldWorkflow = app.context.continuePending(oldPending, false);
  await flush();
  // Simulate another owner replacing the persisted operation before a new workflow.
  app.storage.setItem("ai-rpg-pending-turn", JSON.stringify(newPending));
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

const catalog = {
  scenarios: [{ scenario_ref: "chapel", scenario_version: 2, title: "廃礼拝堂", objective: "鐘を探す" }],
  presets: [
    { preset_ref: "guard", name: "守り手", description: "丈夫な旅人", max_hp: 18 },
    { preset_ref: "scout", name: "斥候", description: "身軽な旅人", max_hp: 12 },
  ],
};
function adventureState(overrides = {}) {
  return {
    state_version: 0, latest_turn: null,
    player: { actor_id: "actor-a", name: "葵", current_hp: 9, max_hp: 12,
      inventory: [{ item_id: "item-1", item_ref: "sword", name: "短剣", quantity: 1, equipped: true }] },
    adventure: { scenario_ref: "chapel", title: "廃礼拝堂", objective: "鐘を探す", status: "active",
      current_scene: { scene_ref: "gate", title: "門前", description: "霧の中に門がある。" },
      discovered_facts: ["扉は閉ざされている"],
      available_actions: [{ action_ref: "look", label: "周囲を見る" }], ending: null },
    ...overrides,
  };
}
const savedAdventure = { campaign_id: "campaign-a", actor_id: "actor-a", player_name: "葵",
  scenario_ref: "chapel", scenario_version: 2, title: "廃礼拝堂", status: "active",
  state_version: 0, created_at: "2026-09-22T00:00:00Z" };
function adventureFetch(handler = () => undefined) {
  return async (path, options = {}) => {
    const handled = await handler(path, options);
    if (handled !== undefined) return handled;
    if (path === "/health") return response({ status: "ok" });
    if (path === "/adventures/catalog") return response(catalog);
    if (path === "/adventures" && !options.method) return response({ adventures: [savedAdventure] });
    if (path.endsWith("/state")) return response(adventureState());
    if (path.includes("/history?")) return response({ items: [], next_before_turn_id: null });
    throw new Error(`unexpected request: ${path}`);
  };
}
function newAdventureApp(handler, options = {}) {
  return harness(adventureFetch(handler), ["start-1", "turn-1", "request-3"], null, { adventureApi: true, ...options });
}
async function startFromForm(app, name = "  葵  ") {
  app.elements["scenario-select"].value = "chapel";
  app.elements["preset-select"].value = "scout";
  app.elements["player-name"].value = name;
  await app.elements["start-form"].dispatch("submit", { preventDefault() {} });
  await flush();
}

test("catalog builds two presets and start renders authoritative player and adventure state", async () => {
  const posts = [];
  const app = newAdventureApp((path, options) => {
    if (path === "/adventures" && options.method === "POST") {
      posts.push(JSON.parse(options.body));
      return response({ campaign_id: "campaign-a", actor_id: "actor-a" }, 201);
    }
  });
  await flush();
  assert.equal(app.elements["preset-select"].children.length, 2);
  assert.deepEqual(app.elements["preset-select"].children.map(node => node.value), ["guard", "scout"]);
  await startFromForm(app);
  assert.deepEqual(posts, [{ request_id: "start-1", scenario_ref: "chapel", scenario_version: 2,
    preset_ref: "scout", player_name: "  葵  " }]);
  assert.equal(app.elements["campaign-id"].value, "campaign-a");
  assert.equal(app.elements["hp-value"].textContent, "9 / 12");
  assert.equal(app.elements["scene-title"].textContent, "門前");
  assert.equal(app.elements["objective-value"].textContent, "鐘を探す");
  assert.ok(find(app.elements["inventory-list"], node => node.textContent.includes("短剣")));
  assert.ok(find(app.elements["facts-list"], node => node.textContent === "扉は閉ざされている"));
  const action = find(app.elements["available-actions"], node => node.textContent === "周囲を見る");
  await action.dispatch("click");
  assert.equal(app.elements["action-text"].value, "周囲を見る");
  assert.equal(posts.length, 1);
});

test("lost start response survives reload and explicit retry uses the exact payload", async () => {
  const posts = [];
  const storage = new MemoryStorage();
  const handler = (path, options) => {
    if (path === "/adventures" && options.method === "POST") {
      posts.push(options.body);
      if (posts.length === 1) throw new TypeError("response lost");
      return response({ campaign_id: "campaign-a", actor_id: "actor-a" }, 201);
    }
  };
  const first = newAdventureApp(handler, { storage });
  await flush();
  await startFromForm(first);
  const reloaded = newAdventureApp(handler, { storage });
  await flush();
  assert.equal(posts.length, 1, "reload must not create another adventure");
  assert.equal(reloaded.elements["retry-start"].hidden, false);
  reloaded.elements["player-name"].value = "changed";
  await reloaded.elements["retry-start"].dispatch("click");
  await flush();
  assert.equal(posts.length, 2);
  assert.equal(posts[1], posts[0]);
  assert.equal(AiRpgPending.loadStart(storage), null);
});

test("accepted start with failed state GET retries selection without another POST", async () => {
  let posts = 0;
  let failState = true;
  const app = newAdventureApp((path, options) => {
    if (path === "/adventures" && options.method === "POST") {
      posts++;
      return response({ campaign_id: "campaign-a", actor_id: "actor-a" }, 201);
    }
    if (path.endsWith("/state") && failState) throw new Error("state unavailable");
  });
  await flush();
  await startFromForm(app);
  assert.equal(posts, 1);
  assert.equal(AiRpgPending.loadStart(app.storage).adventure.campaign_id, "campaign-a");
  failState = false;
  await app.elements["retry-start"].dispatch("click");
  await flush();
  assert.equal(posts, 1);
  assert.equal(app.elements["hp-value"].textContent, "9 / 12");
});

test("storage denied at load or save explains recovery requirement and sends no POST", async () => {
  for (const denyRead of [false, true]) {
    const storage = new MemoryStorage();
    storage.setItem = () => { throw new Error("storage denied"); };
    if (denyRead) storage.getItem = () => { throw new Error("storage denied"); };
    let posts = 0;
    const app = newAdventureApp((path, options) => {
      if (options.method === "POST") { posts++; return response({}); }
    }, { storage });
    await flush();
    await startFromForm(app);
    app.elements["campaign-id"].value = "campaign-a";
    app.elements["actor-id"].value = "actor-a";
    app.elements["action-text"].value = "進む";
    await app.elements.composer.requestSubmit();
    await flush();
    assert.equal(posts, 0);
    assert.ok(find(app.elements.timeline, node => /保存.*送信|送信.*保存/.test(node.textContent)));
  }
});

test("saved adventure restores DB history and prepends older pages with inert old choices", async () => {
  const recent = { ...terminalTurn("turn-2", 2), narration: "今の描写", choices: [{ id: "choice-2", label: "新しい選択" }] };
  const old = { ...terminalTurn("turn-1", 1), narration: "古い描写", choices: [{ id: "choice-1", label: "古い選択" }] };
  const requested = [];
  const storage = new MemoryStorage();
  storage.setItem("ai-rpg-campaign", "campaign-a");
  storage.setItem("ai-rpg-actor", "actor-a");
  const app = newAdventureApp(path => {
    if (path.endsWith("/state")) return response(adventureState({ state_version: 2, latest_turn: recent }));
    if (path.includes("/history?")) {
      requested.push(path);
      return response(path.includes("before_turn_id=turn-2")
        ? { items: [{ created_at: "2026-09-21T00:00:00Z", player_input: "最初の行動", turn: old }], next_before_turn_id: null }
        : { items: [{ created_at: "2026-09-22T00:00:00Z", player_input: "次の行動", turn: recent }], next_before_turn_id: "turn-2" });
    }
  }, { storage });
  await flush();
  assert.ok(find(app.elements.timeline, node => node.textContent === "次の行動"));
  await app.elements["load-history"].dispatch("click");
  await flush();
  const bodies = app.elements.timeline.children.map(node => node.querySelector(".message-body")?.textContent);
  assert.deepEqual(bodies, ["最初の行動", "古い描写", "次の行動", "今の描写"]);
  const oldChoice = find(app.elements.timeline, node => node.textContent === "古い選択");
  assert.ok(!oldChoice || oldChoice.disabled);
  assert.equal(find(app.elements.timeline, node => node.textContent === "新しい選択").disabled, false);
  assert.equal(app.elements["load-history"].hidden, true);
  assert.deepEqual(requested, ["/campaigns/campaign-a/history?limit=50", "/campaigns/campaign-a/history?limit=50&before_turn_id=turn-2"]);
});

test("delayed state and history from an earlier selection cannot overwrite the current adventure", async () => {
  let resolveState, resolveHistory;
  const state = new Promise(resolve => { resolveState = resolve; });
  const history = new Promise(resolve => { resolveHistory = resolve; });
  const app = newAdventureApp(path => {
    if (path === "/campaigns/campaign-a/state") return state;
    if (path.startsWith("/campaigns/campaign-a/history?")) return history;
    if (path === "/campaigns/campaign-b/state") return response(adventureState({
      state_version: 9, player: { ...adventureState().player, actor_id: "actor-b", name: "別の冒険者" },
    }));
  });
  await flush();
  const old = app.context.selectAdventure("campaign-a", "actor-a");
  await flush();
  await app.context.selectAdventure("campaign-b", "actor-b");
  resolveState(response(adventureState()));
  resolveHistory(response({ items: [{ player_input: "古い行動", created_at: "2026-09-21T00:00:00Z", turn: terminalTurn("old", 1) }], next_before_turn_id: "old" }));
  await old;
  assert.equal(app.elements["version-value"].textContent, "9");
  assert.equal(app.elements["player-value"].textContent, "別の冒険者");
  assert.equal(find(app.elements.timeline, node => node.textContent === "古い行動"), null);
});

test("last turn refreshes HP scene ending and disables actions while allowing another adventure", async () => {
  const done = terminalTurn("turn-final", 1);
  let completed = false;
  let posts = 0;
  const app = newAdventureApp((path, options) => {
    if (options.method === "POST") { completed = true; posts++; return response(done, 202); }
    if (path.endsWith("/state") && completed) return response(adventureState({
      state_version: 1, latest_turn: done,
      player: { ...adventureState().player, current_hp: 4 },
      adventure: { ...adventureState().adventure, status: "completed", available_actions: [],
        current_scene: { scene_ref: "exit", title: "出口", description: "帰り道" },
        ending: { ending_ref: "safe", title: "生還", summary: "鐘を持ち帰った。" } },
    }));
  });
  await flush();
  await app.context.selectAdventure("campaign-a", "actor-a");
  app.elements["action-text"].value = "帰る";
  await app.elements.composer.requestSubmit();
  await flush();
  assert.equal(app.elements["hp-value"].textContent, "4 / 12");
  assert.equal(app.elements["scene-title"].textContent, "出口");
  assert.equal(app.elements["ending-title"].textContent, "生還");
  assert.equal(app.elements["send-action"].disabled, true);
  assert.equal(app.elements["start-adventure"].disabled, false);
  await app.elements.composer.requestSubmit();
  await flush();
  assert.equal(posts, 1);
});

test("401 remains visible after a late health success and never automatically retries POST", async () => {
  let resolveHealth;
  const health = new Promise(resolve => { resolveHealth = resolve; });
  let posts = 0;
  const app = newAdventureApp((path, options) => {
    if (path === "/health") return health;
    if (options.method === "POST") {
      posts++;
      return response({ detail: { code: "UNAUTHENTICATED" } }, 401);
    }
  });
  await flush();
  await startFromForm(app);
  resolveHealth(response({ status: "ok" }));
  await flush();
  assert.equal(app.elements["connection-label"].textContent, "認証が必要");
  assert.equal(AiRpgPending.loadStart(app.storage), null);
  assert.equal(posts, 1);
});

test("late start acceptance does not replace a newer selected adventure", async () => {
  let resolveStart;
  const accepted = new Promise(resolve => { resolveStart = resolve; });
  const app = newAdventureApp((path, options) => {
    if (path === "/adventures" && options.method === "POST") return accepted;
  });
  await flush();
  const starting = startFromForm(app);
  await flush();
  await app.context.selectAdventure("campaign-b", "actor-b");
  resolveStart(response({ campaign_id: "campaign-a", actor_id: "actor-a" }, 201));
  await starting;
  assert.equal(app.elements["campaign-id"].value, "campaign-b");
  assert.equal(AiRpgPending.loadStart(app.storage).adventure.campaign_id, "campaign-a");
  assert.equal(app.elements["retry-start"].hidden, false);
});

test("late history page is ignored after switching adventures", async () => {
  let resolveHistory;
  const history = new Promise(resolve => { resolveHistory = resolve; });
  const app = newAdventureApp(path => {
    if (path.startsWith("/campaigns/campaign-a/history?")) return history;
  });
  await flush();
  const oldSelection = app.context.selectAdventure("campaign-a", "actor-a");
  await flush();
  await app.context.selectAdventure("campaign-b", "actor-b");
  resolveHistory(response({ items: [{ player_input: "old history", created_at: "2026-09-21T00:00:00Z", turn: terminalTurn("old", 1) }], next_before_turn_id: "old" }));
  await oldSelection;
  assert.equal(find(app.elements.timeline, node => node.textContent === "old history"), null);
  assert.equal(app.elements["load-history"].hidden, true);
});

test("reload of an unknown turn offers an explicit replay and the used retry cannot replay again", async () => {
  const storage = new MemoryStorage();
  const pending = AiRpgPending.create({ campaignId: "campaign-a", actorId: "actor-a", displayText: "調べる",
    content: { kind: "text", text: "調べる" }, stateVersion: 0, requestId: "old-request" });
  AiRpgPending.save(storage, pending);
  const posts = [];
  const app = newAdventureApp((path, options) => {
    if (options.method === "POST") { posts.push(JSON.parse(options.body)); return response(terminalTurn("turn-1", 0), 202); }
  }, { storage });
  await flush();
  assert.equal(posts.length, 0);
  const retry = find(app.elements.timeline, node => node.textContent === "同じ送信を再試行");
  assert.ok(retry);
  await retry.dispatch("click");
  await flush();
  assert.deepEqual(posts, [pending.body]);
  await retry.dispatch("click");
  await flush();
  assert.equal(posts.length, 1);
});

test("422 start clears pending while 503 preserves its exact payload for retry", async () => {
  for (const status of [422, 503]) {
    const posts = [];
    const app = newAdventureApp((path, options) => {
      if (path === "/adventures" && options.method === "POST") {
        posts.push(options.body);
        if (posts.length === 1) return response({ detail: { code: "UNKNOWN" } }, status);
        return response({ campaign_id: "campaign-a", actor_id: "actor-a" }, 201);
      }
    });
    await flush();
    await startFromForm(app);
    if (status === 422) {
      assert.equal(AiRpgPending.loadStart(app.storage), null);
      await startFromForm(app, "別の名前");
      assert.notEqual(JSON.parse(posts[1]).request_id, JSON.parse(posts[0]).request_id);
    } else {
      assert.ok(AiRpgPending.loadStart(app.storage));
      await app.elements["retry-start"].dispatch("click");
      assert.equal(posts[1], posts[0]);
    }
  }
});

test("completed turn with state failure uses GET-only recovery", async () => {
  let posts = 0;
  let failState = false;
  const app = newAdventureApp((path, options) => {
    if (options.method === "POST") { posts++; failState = true; return response(terminalTurn("turn-done", 1), 202); }
    if (path.endsWith("/state") && failState) throw new Error("state failed");
  });
  await flush();
  await app.context.selectAdventure("campaign-a", "actor-a");
  app.elements["action-text"].value = "調べる";
  await app.elements.composer.requestSubmit();
  await flush();
  assert.equal(AiRpgPending.load(app.storage), null);
  assert.equal(app.elements["send-action"].disabled, true);
  const retry = find(app.elements.timeline, node => node.textContent === "状態を再取得");
  assert.ok(retry);
  failState = false;
  await retry.dispatch("click");
  await flush();
  assert.equal(posts, 1);
  assert.equal(app.elements["send-action"].disabled, false);
});

test("accepted start reload opens its saved adventure and releases the start form without POST", async () => {
  const storage = new MemoryStorage();
  AiRpgPending.saveStart(storage, { body: { request_id: "accepted-start", scenario_ref: "chapel",
    scenario_version: 2, preset_ref: "scout", player_name: "葵" },
    adventure: { campaign_id: "campaign-a", actor_id: "actor-a" } });
  storage.setItem("ai-rpg-campaign", "campaign-a");
  storage.setItem("ai-rpg-actor", "actor-a");
  let posts = 0;
  const app = newAdventureApp((path, options) => { if (options.method === "POST") posts++; }, { storage });
  await flush();
  assert.equal(posts, 0);
  assert.equal(AiRpgPending.loadStart(storage), null);
  assert.equal(app.elements["start-adventure"].disabled, false);
  assert.equal(app.elements["hp-value"].textContent, "9 / 12");
});

test("DB pending turn resumes through polling and refreshes public state without POST", async () => {
  const storage = new MemoryStorage();
  storage.setItem("ai-rpg-campaign", "campaign-a");
  storage.setItem("ai-rpg-actor", "actor-a");
  const progress = pendingTurn("in-progress");
  let finished = false;
  let posts = 0;
  const app = newAdventureApp((path, options) => {
    if (options.method === "POST") posts++;
    if (path.endsWith("/state")) return response(adventureState({ state_version: finished ? 1 : 0,
      latest_turn: finished ? terminalTurn("in-progress", 1) : progress,
      player: { ...adventureState().player, current_hp: finished ? 6 : 9 } }));
    if (path.includes("/history?")) return response({ items: [{ created_at: "2026-09-22T00:00:00Z",
      player_input: "戦う", turn: progress }], next_before_turn_id: null });
    if (path.endsWith("/turns/in-progress")) { finished = true; return response(terminalTurn("in-progress", 1)); }
  }, { storage });
  await flush();
  assert.equal(posts, 0);
  assert.equal(app.elements["hp-value"].textContent, "6 / 12");
  assert.ok(find(app.elements.timeline, node => node.textContent === "戦う"));
  assert.ok(find(app.elements.timeline, node => node.textContent === "続行した。"));
  assert.equal(app.elements["send-action"].disabled, false);
});

function storyMessages(app) {
  return app.elements.timeline.children
    .filter(node => ["message player", "message gm"].includes(node.className))
    .map(node => [node.children[0].textContent, node.querySelector(".message-body").textContent]);
}

test("P1 unused retry for A cannot overwrite or clear later ambiguous request B", async () => {
  const storage = new MemoryStorage();
  const pendingA = AiRpgPending.create({ campaignId: "campaign-a", actorId: "actor-a", displayText: "old input",
    content: { kind: "text", text: "old input" }, stateVersion: 0, requestId: "request-a" });
  AiRpgPending.save(storage, pendingA);
  const posts = [];
  const app = newAdventureApp((path, options) => {
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      posts.push(body);
      if (body.request_id !== "request-a") throw new Error("B response lost");
      return response(terminalTurn("turn-a", 0), 202);
    }
  }, { storage });
  await flush();
  const unusedRetry = find(app.elements.timeline, node => node.textContent === "同じ送信を再試行");
  app.elements["action-text"].value = "create another retry for A";
  await app.elements.composer.requestSubmit();
  await flush();
  const otherRetry = find(app.elements.timeline, node => node !== unusedRetry && node.textContent === "同じ送信を再試行");
  assert.ok(otherRetry);
  await otherRetry.dispatch("click");
  await flush();
  app.elements["action-text"].value = "new input";
  await app.elements.composer.requestSubmit();
  await flush();
  const pendingB = AiRpgPending.load(storage);
  assert.equal(pendingB.body.content.text, "new input");
  await unusedRetry.dispatch("click");
  await flush();
  assert.deepEqual(posts.map(body => body.request_id), ["request-a", "start-1"]);
  assert.deepEqual(AiRpgPending.load(storage), pendingB);
  assert.equal(unusedRetry.disabled, true);
});

test("P1 delayed acceptance or rejection cannot mutate a replacement pending slot", async () => {
  for (const outcome of [202, 422, 409]) {
    const storage = new MemoryStorage();
    let resolvePost;
    const posted = new Promise(resolve => { resolvePost = resolve; });
    const app = newAdventureApp((path, options) => options.method === "POST" ? posted : undefined, { storage });
    await flush();
    await app.context.selectAdventure("campaign-a", "actor-a");
    app.elements["action-text"].value = "old input";
    await app.elements.composer.requestSubmit();
    await flush();
    const replacement = AiRpgPending.create({ campaignId: "campaign-a", actorId: "actor-a", displayText: "replacement",
      content: { kind: "text", text: "replacement" }, stateVersion: 0, requestId: "replacement-request" });
    // Another page owns the slot now; deliberately bypass the production writer.
    storage.setItem("ai-rpg-pending-turn", JSON.stringify(replacement));
    resolvePost(outcome === 202 ? response(terminalTurn("old-turn", 0), 202)
      : response({ detail: { code: outcome === 409 ? "STATE_VERSION_CONFLICT" : "UNKNOWN" } }, outcome));
    await flush();
    assert.deepEqual(AiRpgPending.load(storage), replacement, `status ${outcome}`);
    assert.equal(find(app.elements.timeline, node => node.textContent === "続行した。"), null);
  }
});

test("P2 unknown acceptance replay renders its original input exactly once with its output", async () => {
  const storage = new MemoryStorage();
  AiRpgPending.save(storage, AiRpgPending.create({ campaignId: "campaign-a", actorId: "actor-a", displayText: "original input",
    content: { kind: "text", text: "original input" }, stateVersion: 0, requestId: "unknown-request" }));
  const app = newAdventureApp((path, options) => options.method === "POST"
    ? response({ ...terminalTurn("recovered", 1), narration: "recovered output" }, 202) : undefined, { storage });
  await flush();
  await find(app.elements.timeline, node => node.textContent === "同じ送信を再試行").dispatch("click");
  await flush();
  assert.deepEqual(storyMessages(app), [["YOU", "original input"], ["GM", "recovered output"]]);
});

test("P2 latest history retry reconciles complete input-output pairs after live play", async () => {
  const old = { ...terminalTurn("turn-old", 0), narration: "old output" };
  const next = { ...terminalTurn("turn-new", 0), narration: "new output" };
  let latest = old;
  let historyCalls = 0;
  const app = newAdventureApp((path, options) => {
    if (path.endsWith("/state")) return response(adventureState({ latest_turn: latest }));
    if (path.includes("/history?")) {
      if (++historyCalls === 1) throw new Error("history unavailable");
      return response({ items: [
        { created_at: "2026-09-22T00:00:00Z", player_input: "old input", turn: old },
        { created_at: "2026-09-22T00:01:00Z", player_input: "new input", turn: next },
      ], next_before_turn_id: null });
    }
    if (options.method === "POST") { latest = next; return response(next, 202); }
  });
  await flush();
  await app.context.selectAdventure("campaign-a", "actor-a");
  app.elements["action-text"].value = "new input";
  await app.elements.composer.requestSubmit();
  await flush();
  await app.elements["load-history"].dispatch("click");
  await flush();
  assert.deepEqual(storyMessages(app), [
    ["YOU", "old input"], ["GM", "old output"], ["YOU", "new input"], ["GM", "new output"],
  ]);
});

test("P2 history reconciliation preserves unfinished DB input and unaccepted local input", async () => {
  const old = { ...terminalTurn("turn-old", 0), narration: "old output" };
  let historyCalls = 0;
  const app = newAdventureApp((path, options) => {
    if (path.endsWith("/state")) return response(adventureState({ latest_turn: old }));
    if (path.includes("/history?")) {
      if (++historyCalls === 1) throw new Error("history unavailable");
      return response({ items: [
        { created_at: "2026-09-22T00:00:00Z", player_input: "old input", turn: old },
        { created_at: "2026-09-22T00:01:00Z", player_input: "unfinished input", turn: pendingTurn("unfinished") },
      ], next_before_turn_id: null });
    }
    if (options.method === "POST") throw new Error("unaccepted local request");
  });
  await flush();
  await app.context.selectAdventure("campaign-a", "actor-a");
  app.elements["action-text"].value = "local input";
  await app.elements.composer.requestSubmit();
  await flush();
  const local = AiRpgPending.load(app.storage);
  await app.elements["load-history"].dispatch("click");
  await flush();
  assert.deepEqual(storyMessages(app), [
    ["YOU", "old input"], ["GM", "old output"], ["YOU", "unfinished input"], ["YOU", "local input"],
  ]);
  assert.deepEqual(AiRpgPending.load(app.storage), local);
});

test("P2 delayed unfinished history keeps the live completed pair and current choices", async () => {
  const events = eventSourceTracker();
  const old = { ...terminalTurn("old", 0), narration: "old output" };
  const completed = { ...terminalTurn("live", 1), narration: "live output", choices: [{ id: "next", label: "current choice" }] };
  let latest = old;
  let historyCalls = 0;
  let resolveHistory;
  const delayedHistory = new Promise(resolve => { resolveHistory = resolve; });
  const app = newAdventureApp((path, options) => {
    if (path.endsWith("/state")) return response(adventureState({ state_version: latest === completed ? 1 : 0, latest_turn: latest }));
    if (path.includes("/history?")) {
      if (++historyCalls === 1) throw new Error("history unavailable");
      return delayedHistory;
    }
    if (options.method === "POST") return response(pendingTurn("live"), 202);
  }, events);
  await flush();
  await app.context.selectAdventure("campaign-a", "actor-a");
  app.elements["action-text"].value = "live input";
  await app.elements.composer.requestSubmit();
  await flush();
  const loading = app.elements["load-history"].dispatch("click");
  latest = completed;
  events.instances[0].emit("turn.updated", completed);
  await flush();
  resolveHistory(response({ items: [
    { created_at: "2026-09-22T00:00:00Z", player_input: "old input", turn: old },
    { created_at: "2026-09-22T00:01:00Z", player_input: "live input", turn: pendingTurn("live") },
  ], next_before_turn_id: null }));
  await loading;
  assert.deepEqual(storyMessages(app), [
    ["YOU", "old input"], ["GM", "old output"], ["YOU", "live input"], ["GM", "live output"],
  ]);
  assert.equal(find(app.elements.timeline, node => node.textContent === "current choice").disabled, false);
  assert.equal(AiRpgPending.load(app.storage), null);
});
