const test = require("node:test");
const assert = require("node:assert/strict");

const pendingState = require("../../src/ai_rpg/api/static/play-state.js");

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, value); }
  removeItem(key) { this.values.delete(key); }
}

test("unknown POST result reloads the exact same request body", () => {
  const storage = new MemoryStorage();
  const pending = pendingState.create({
    campaignId: "campaign-a",
    actorId: "actor-a",
    displayText: "調べる",
    content: { kind: "text", text: "調べる" },
    stateVersion: 7,
    requestId: "request-fixed",
  });

  pendingState.save(storage, pending);
  const restored = pendingState.load(storage);

  assert.deepEqual(restored, pending);
  assert.equal(restored.body.request_id, "request-fixed");
  assert.equal(restored.body.expected_state_version, 7);
});

test("accepted turn id is retained for reload tracking without changing POST body", () => {
  const storage = new MemoryStorage();
  const pending = pendingState.create({
    campaignId: "campaign-a",
    actorId: "actor-a",
    displayText: "進む",
    content: { kind: "text", text: "進む" },
    stateVersion: 2,
    requestId: "request-fixed",
  });
  const body = JSON.stringify(pending.body);

  pendingState.save(storage, { ...pending, turnId: "turn-known" });
  const restored = pendingState.load(storage);

  assert.equal(restored.turnId, "turn-known");
  assert.equal(JSON.stringify(restored.body), body);
});

test("clearing a rejected operation leaves no stale retry", () => {
  const storage = new MemoryStorage();
  pendingState.save(storage, pendingState.create({
    campaignId: "campaign-b",
    actorId: "actor-b",
    displayText: "進む",
    content: { kind: "text", text: "進む" },
    stateVersion: 0,
    requestId: "request-b",
  }));

  pendingState.clear(storage, pendingState.load(storage));

  assert.equal(pendingState.load(storage), null);
});

test("start recovery persists the exact payload separately from pending turns", () => {
  const storage = new MemoryStorage();
  const start = { body: {
    request_id: "start-1", scenario_ref: "chapel", scenario_version: 2,
    preset_ref: "scout", player_name: "  葵  ",
  }, adventure: null };
  pendingState.saveStart(storage, start);
  pendingState.clear(storage, pendingState.load(storage));
  assert.deepEqual(pendingState.loadStart(storage), start);
  pendingState.saveStart(storage, { ...start, adventure: { campaign_id: "c1", actor_id: "a1" } });
  assert.equal(pendingState.loadStart(storage).adventure.campaign_id, "c1");
  pendingState.clearStart(storage);
  assert.equal(pendingState.loadStart(storage), null);
});

test("pending save and clear require the expected operation identity", () => {
  const storage = new MemoryStorage();
  const old = pendingState.create({ campaignId: "campaign-a", actorId: "actor-a", displayText: "old",
    content: { kind: "text", text: "old" }, stateVersion: 0, requestId: "request-old" });
  const next = pendingState.create({ campaignId: "campaign-a", actorId: "actor-a", displayText: "next",
    content: { kind: "text", text: "next" }, stateVersion: 0, requestId: "request-next" });
  pendingState.save(storage, next);
  pendingState.save(storage, { ...old, turnId: "old-turn" }, old);
  assert.deepEqual(pendingState.load(storage), next);
  pendingState.clear(storage, old);
  assert.deepEqual(pendingState.load(storage), next);
  pendingState.save(storage, old);
  assert.deepEqual(pendingState.load(storage), next, "claim requires an empty slot");
  pendingState.clear(storage, next);
  pendingState.save(storage, { ...old, turnId: "old-turn" }, old);
  assert.equal(pendingState.load(storage), null, "obsolete update cannot resurrect a cleared slot");
});
