"use strict";

const assert = require("assert");
const Notifications = require("../src/rekit_factory/ui/mission-notifications.js");

assert.deepStrictEqual(Notifications.exactRoute({view: "mission-control", runId: "run-1",
  tab: "findings", entityType: "finding", entityId: "finding-1"}),
{runId: "run-1", entityType: "finding", entityId: "finding-1", surface: "outcomes"});
assert.deepStrictEqual(Notifications.exactRoute({view: "mission-control", runId: "run-1",
  tab: "decisions", entityType: "operator-decision", entityId: "question-1"}),
{runId: "run-1", entityType: "operator-decision", entityId: "question-1", surface: "decisions"});
assert.deepStrictEqual(Notifications.exactRoute({view: "mission-control", runId: "run-1",
  tab: "dossiers", entityType: "proof-bundle", entityId: "dossier-1"}),
{runId: "run-1", entityType: "proof-bundle", entityId: "dossier-1", surface: "dossiers"});
assert.deepStrictEqual(Notifications.exactRoute({view: "mission-control", tab: "campaigns",
  entityType: "campaign", entityId: "campaign-1"}),
{entityType: "campaign", entityId: "campaign-1", surface: "campaigns"});

for (const forged of [
  null,
  {view: "mission-control", runId: "run-1", tab: "outcomes", entityType: "finding", entityId: "finding-1"},
  {view: "mission-control", runId: "run-1", tab: "decisions", entityType: "finding", entityId: "finding-1"},
  {view: "mission-control", runId: "run-1", tab: "findings", entityType: "finding", entityId: "../finding"},
  {view: "mission-control", runId: "run-1", tab: "findings", entityType: "finding", entityId: "finding-1", extra: true},
]) assert.strictEqual(Notifications.exactRoute(forged), null);

console.log("mission notification routes adversarial: ok");
