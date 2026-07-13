"use strict";

const assert = require("node:assert/strict");
const {claimQuestionState, createTracker, focusInbox, messageFor, restoreFocus, shouldFocusInbox} = require("../src/rekit_factory/ui/mission-attention.js");

const run = (runId, needsYou) => ({runId, needsYou});
const tracker = createTracker();

assert.deepEqual(tracker.transitions([run("run-a", 2)]), [], "initial positive state is only a baseline");
assert.deepEqual(tracker.transitions([run("run-a", 2)]), [], "an unchanged reconnect does not alert");
assert.deepEqual(tracker.transitions([run("run-a", 0)]), []);
assert.deepEqual(tracker.transitions([run("run-a", 1)]).map(item => item.runId), ["run-a"]);
assert.deepEqual(tracker.transitions([run("run-a", 2)]), [], "only zero-to-positive transitions alert");

assert.equal(tracker.claim("run-a", ["question-2", "question-1"]), true);
assert.equal(tracker.claim("run-a", ["question-1", "question-2"]), false, "question order cannot bypass dedupe");
assert.equal(tracker.claim("run-a", ["question-3"]), true, "new question state is independently claimable");
assert.equal(tracker.claim("run-a", []), false, "an unanchored alert is never claimed");

tracker.transitions([run("run-a", 0)]);
assert.equal(tracker.transitions([run("run-a", 1)]).length, 1);
assert.equal(tracker.claim("run-a", ["question-1", "question-2"]), false, "the same resolved-and-returned state remains deduped");

tracker.rearm("run-a");
assert.equal(tracker.transitions([run("run-a", 1)]).length, 1, "snapshot failures can retry on a later live refresh");

assert.equal(messageFor(1, 1), "A new decision is waiting in the Decision Inbox.");
assert.equal(messageFor(1, 2), "2 new decisions are waiting across 1 investigation.");
assert.equal(messageFor(2, 3), "3 new decisions are waiting across 2 investigations.");

const emptySnapshotTracker = createTracker();
emptySnapshotTracker.transitions([run("run-empty", 0)]);
assert.equal(emptySnapshotTracker.transitions([run("run-empty", 1)]).length, 1);
assert.equal(claimQuestionState(emptySnapshotTracker, "run-empty", []), 0);
assert.equal(emptySnapshotTracker.transitions([run("run-empty", 1)]).length, 1, "an empty detail snapshot rearms the live transition");
assert.equal(claimQuestionState(emptySnapshotTracker, "run-empty", [{id: "question-live", prompt: "secret"}]), 1);
assert.equal(claimQuestionState(emptySnapshotTracker, "run-empty", [{id: "question-live", prompt: "changed"}]), 0, "dedupe uses question identity, not payload details");

const focusCalls = [], focusDocument = {activeElement: null};
const focusable = (name, attributes = {}) => ({
  isConnected: attributes.isConnected ?? true,
  hidden: attributes.hidden ?? false,
  getAttribute: key => attributes[key] ?? null,
  closest: () => attributes.hiddenAncestor ? {} : null,
  focus(options) {
    focusCalls.push([name, options]);
    if (attributes.throws) throw new Error("focus failed");
    if (!attributes.noMove) focusDocument.activeElement = this;
  },
});
const priorControl = focusable("prior");
assert.equal(restoreFocus(priorControl, focusDocument), priorControl);
assert.deepEqual(focusCalls.pop(), ["prior", {preventScroll: true}]);
const fallbackHeading = focusable("heading");
assert.equal(restoreFocus(focusable("detached", {isConnected: false}), focusDocument, [fallbackHeading]), fallbackHeading);
assert.deepEqual(focusCalls.pop(), ["heading", {preventScroll: true}], "detached focus falls back to the active heading");
const fallbackNav = focusable("nav");
assert.equal(restoreFocus(focusable("hidden", {hidden: true}), focusDocument, [focusable("failed", {throws: true}), fallbackNav]), fallbackNav);
assert.deepEqual(focusCalls.pop(), ["nav", {preventScroll: true}], "hidden and failed targets fall back to active navigation");
assert.equal(restoreFocus(null, focusDocument, [fallbackHeading]), fallbackHeading, "no prior focus uses a stable fallback");
assert.equal(restoreFocus(focusable("no-move", {noMove: true}), focusDocument, [fallbackNav]), fallbackNav, "focus must actually update activeElement");

const firstAction = {focus: options => focusCalls.push(["action", options])};
const inboxHeading = {focus: options => focusCalls.push(["heading", options])};
assert.equal(focusInbox({querySelector: () => firstAction}, inboxHeading), firstAction);
assert.deepEqual(focusCalls.pop(), ["action", {preventScroll: true}]);
assert.equal(focusInbox({querySelector: () => null}, inboxHeading), inboxHeading);
assert.deepEqual(focusCalls.pop(), ["heading", {preventScroll: true}]);

let resolveInbox;
let currentGeneration = 1;
let inboxActive = true;
let deferredFocusCount = 0;
const deferredInbox = new Promise(resolve => { resolveInbox = resolve; });
const deferredFocus = deferredInbox.then(() => {
  const view = {classList: {contains: name => name === "active" && inboxActive}};
  if (shouldFocusInbox(1, currentGeneration, view)) deferredFocusCount += 1;
});
currentGeneration = 2;
inboxActive = false;
resolveInbox();
deferredFocus.then(() => {
  assert.equal(deferredFocusCount, 0, "a deferred Inbox load cannot steal focus after navigation away");
  console.log("mission attention tracker: ok");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
