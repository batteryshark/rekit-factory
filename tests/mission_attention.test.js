"use strict";

const assert = require("node:assert/strict");
const {createTracker} = require("../src/rekit_factory/ui/mission-attention.js");

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

console.log("mission attention tracker: ok");
