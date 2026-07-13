"use strict";

(function exposeMissionAttention(root, factory) {
  const attention = factory();
  if (typeof module === "object" && module.exports) module.exports = attention;
  root.MissionAttention = attention;
})(typeof globalThis === "object" ? globalThis : this, () => {
  const countFor = run => Math.max(0, Number.isFinite(Number(run?.needsYou)) ? Number(run.needsYou) : 0);

  function createTracker({seenLimit = 512} = {}) {
    const limit = Number.isInteger(seenLimit) && seenLimit > 0 ? seenLimit : 512;
    let initialized = false;
    const previous = new Map();
    const seen = new Set();

    function transitions(runs = []) {
      const next = new Map(runs.map(run => [String(run.runId), countFor(run)]));
      if (!initialized) {
        initialized = true;
        next.forEach((count, runId) => previous.set(runId, count));
        return [];
      }
      const changed = runs.filter(run => (previous.get(String(run.runId)) || 0) === 0 && countFor(run) > 0);
      previous.clear();
      next.forEach((count, runId) => previous.set(runId, count));
      return changed;
    }

    function claim(runId, questionIds = []) {
      const questions = [...new Set(questionIds.map(String).filter(Boolean))].sort();
      if (!questions.length) return false;
      const key = JSON.stringify([String(runId), questions]);
      if (seen.has(key)) return false;
      seen.add(key);
      while (seen.size > limit) seen.delete(seen.values().next().value);
      return true;
    }

    function rearm(runId) {
      if (initialized) previous.set(String(runId), 0);
    }

    return {claim, rearm, transitions};
  }

  return {createTracker};
});
