(function (root) {
  "use strict";

  const key = "ai-rpg-pending-turn";
  const startKey = "ai-rpg-pending-start";

  function create({ campaignId, actorId, displayText, content, stateVersion, requestId }) {
    return {
      campaignId,
      actorId,
      displayText: displayText.trim(),
      turnId: null,
      body: {
        request_id: requestId,
        expected_state_version: stateVersion,
        actor_id: actorId,
        content,
      },
    };
  }

  function load(storage) {
    try {
      const value = JSON.parse(storage.getItem(key));
      if (!value || typeof value !== "object" || typeof value.campaignId !== "string" ||
          typeof value.actorId !== "string" || typeof value.displayText !== "string" ||
          !value.body || typeof value.body !== "object" ||
          typeof value.body.request_id !== "string" ||
          !Number.isInteger(value.body.expected_state_version)) return null;
      return value;
    } catch (_) {
      return null;
    }
  }

  function sameOperation(left, right) {
    if (!left || !right) return left === right;
    return left.campaignId === right.campaignId && left.actorId === right.actorId &&
      JSON.stringify(left.body) === JSON.stringify(right.body);
  }

  function isCurrent(storage, value) {
    return !!value && sameOperation(load(storage), value);
  }

  function save(storage, value, expected = null) {
    if (!sameOperation(load(storage), expected) || (expected && !sameOperation(value, expected))) return false;
    storage.setItem(key, JSON.stringify(value));
    return true;
  }

  function clear(storage, expected) {
    if (!isCurrent(storage, expected)) return false;
    storage.removeItem(key);
    return true;
  }

  function loadStart(storage) {
    try {
      const value = JSON.parse(storage.getItem(startKey));
      const body = value?.body;
      if (!body || typeof body.request_id !== "string" ||
          typeof body.scenario_ref !== "string" || !Number.isInteger(body.scenario_version) ||
          typeof body.preset_ref !== "string" || typeof body.player_name !== "string") return null;
      if (value.adventure && (typeof value.adventure.campaign_id !== "string" ||
          typeof value.adventure.actor_id !== "string")) return null;
      return value;
    } catch (_) {
      return null;
    }
  }

  function saveStart(storage, value) {
    storage.setItem(startKey, JSON.stringify(value));
  }

  function clearStart(storage) {
    storage.removeItem(startKey);
  }

  const api = Object.freeze({ create, load, save, clear, isCurrent, loadStart, saveStart, clearStart });
  root.AiRpgPending = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
