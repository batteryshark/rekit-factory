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

const contract = {schemaVersion: 1, queryMarker: "mc-v1", maxLength: 512, routes: [
  {entityType: "campaign", surface: "campaigns", requiresRun: false},
  {entityType: "finding", surface: "outcomes", requiresRun: true},
  {entityType: "operator-decision", surface: "decisions", requiresRun: true},
  {entityType: "proof-bundle", surface: "dossiers", requiresRun: true},
]};
const findingRoute = {runId: "run-1", entityType: "finding",
  entityId: "finding-1", surface: "outcomes"};
const search = Notifications.urlSearch(findingRoute, contract);
assert.strictEqual(search, "?mc=mc-v1&tab=outcomes&type=finding&entity=finding-1&run=run-1");
assert.deepStrictEqual(Notifications.parseUrlRoute(search, contract), findingRoute);
assert.deepStrictEqual(Notifications.parseUrlRoute(
  "?mc=mc-v1&tab=campaigns&type=campaign&entity=campaign-1", contract,
), {entityType: "campaign", entityId: "campaign-1", surface: "campaigns"});

for (const forged of [
  "?mc=mc-v1&tab=outcomes&type=finding&entity=finding-1",
  "?mc=mc-v1&tab=decisions&type=finding&entity=finding-1&run=run-1",
  "?mc=mc-v1&tab=outcomes&type=finding&entity=finding-1&run=run-1&extra=x",
  "?mc=mc-v1&tab=outcomes&type=finding&entity=..%2Fsecret&run=run-1",
  "?mc=mc-v1&mc=mc-v1&tab=outcomes&type=finding&entity=finding-1&run=run-1",
]) assert.strictEqual(Notifications.parseUrlRoute(forged, contract), null);
assert.strictEqual(Notifications.parseUrlRoute(search, {...contract, maxLength: 999}), null);
assert.strictEqual(Notifications.parseUrlRoute(search, {...contract,
  routes: contract.routes.map((route, index) => index ? route : {...route, surface: "outcomes"})}), null);

const snapshot = {run: {id: "run-1"}, outcomeProjection: {degraded: false, entities: [
  {entityType: "finding", entityId: "finding-1"},
  {entityType: "operator-decision", entityId: "question-1"},
  {entityType: "proof-bundle", entityId: "dossier-1"},
]}, pendingQuestions: [{id: "question-1"}], dossiers: [{id: "dossier-1"}]};
assert.strictEqual(Notifications.canonicalTarget(findingRoute, snapshot, []), true);
assert.strictEqual(Notifications.canonicalTarget({runId: "run-1", entityType: "operator-decision",
  entityId: "question-1", surface: "decisions"}, snapshot, []), true);
assert.strictEqual(Notifications.canonicalTarget({runId: "run-1", entityType: "proof-bundle",
  entityId: "dossier-1", surface: "dossiers"}, snapshot, []), true);
assert.strictEqual(Notifications.canonicalTarget({entityType: "campaign", entityId: "campaign-1",
  surface: "campaigns"}, null, [{campaignId: "campaign-1", health: {degraded: false}}]), true);
for (const [route, context] of [
  [{...findingRoute, runId: "run-other"}, snapshot],
  [{...findingRoute, entityType: "operator-decision"}, snapshot],
  [{...findingRoute, entityId: "finding-stale"}, snapshot],
  [findingRoute, {...snapshot, outcomeProjection: {...snapshot.outcomeProjection, degraded: true}}],
  [{runId: "run-1", entityType: "operator-decision", entityId: "question-1", surface: "decisions"}, {...snapshot, pendingQuestions: []}],
  [{runId: "run-1", entityType: "proof-bundle", entityId: "dossier-1", surface: "dossiers"}, {...snapshot, dossiers: []}],
]) assert.strictEqual(Notifications.canonicalTarget(route, context, []), false);

console.log("mission notification routes adversarial: ok");
