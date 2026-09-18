(function (root) {
  "use strict";

  const key = "ai-rpg-pending-turn";

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

  function save(storage, value) {
    storage.setItem(key, JSON.stringify(value));
  }

  function clear(storage) {
    storage.removeItem(key);
  }

  const api = Object.freeze({ create, load, save, clear });
  root.AiRpgPending = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
