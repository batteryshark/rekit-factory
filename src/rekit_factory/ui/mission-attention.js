"use strict";

(function exposeMissionAttention(root, factory) {
  const attention = factory();
  if (typeof module === "object" && module.exports) module.exports = attention;
  root.MissionAttention = attention;
})(typeof globalThis === "object" ? globalThis : this, () => {
  const countFor = run => Math.max(0, Number.isFinite(Number(run?.needsYou)) ? Number(run.needsYou) : 0);

  function messageFor(runCount, questionCount) {
    const runs = Math.max(1, Number.isFinite(Number(runCount)) ? Number(runCount) : 1);
    const questions = Math.max(1, Number.isFinite(Number(questionCount)) ? Number(questionCount) : 1);
    if (questions === 1) return "A new decision is waiting in the Decision Inbox.";
    return `${questions} new decisions are waiting across ${runs} ${runs === 1 ? "investigation" : "investigations"}.`;
  }

  function claimQuestionState(tracker, runId, pendingQuestions = []) {
    const questionIds = pendingQuestions.map(question => question?.id).filter(Boolean);
    if (!questionIds.length) {
      tracker.rearm(runId);
      return 0;
    }
    return tracker.claim(runId, questionIds) ? questionIds.length : 0;
  }

  function focusTarget(element, documentObject) {
    if (!element?.isConnected || typeof element.focus !== "function") return false;
    if (element.hidden || element.getAttribute?.("aria-hidden") === "true") return false;
    if (element.closest?.("[hidden], [aria-hidden='true']")) return false;
    const style = documentObject?.defaultView?.getComputedStyle?.(element);
    if (style?.display === "none" || style?.visibility === "hidden") return false;
    try {
      element.focus({preventScroll: true});
    } catch (_error) {
      return false;
    }
    return !documentObject || documentObject.activeElement === element;
  }

  function restoreFocus(element, documentObject, fallbacks = []) {
    for (const candidate of [element, ...fallbacks]) {
      if (focusTarget(candidate, documentObject)) return candidate;
    }
    return null;
  }

  function focusInbox(container, heading) {
    const action = container?.querySelector?.("[data-answer], [data-direction-input], [data-direction-submit]");
    const target = action || heading;
    if (typeof target?.focus !== "function") return null;
    target.focus({preventScroll: true});
    return target;
  }

  function shouldFocusInbox(expectedGeneration, currentGeneration, inboxView) {
    return expectedGeneration === currentGeneration && Boolean(
      inboxView?.classList?.contains("active")
    );
  }

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

  return {
    claimQuestionState, createTracker, focusInbox, messageFor, restoreFocus, shouldFocusInbox,
  };
});
