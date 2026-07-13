"use strict";

const assert = require("assert");
const Campaigns = require("../src/rekit_factory/ui/mission-campaigns.js");

const campaign = {
  campaignId: "campaign-safe", status: "running", health: {degraded: false},
  scope: {scopeId: "scope-safe", revision: 1, digest: "a".repeat(64)},
  handoff: {evidenceIds: [], factoryRunIds: [], evidenceCount: 0, factoryRunCount: 0},
  typedLinks: {schemaVersion: 1, totalCount: 9, truncated: false, references: [
    {kind: "evidence", entityId: "evidence-a", runId: "run-a", surface: "artifacts"},
    {kind: "hypothesis", entityId: "hypothesis-a", runId: "run-a", surface: "outcomes"},
    {kind: "finding", entityId: "finding-a", runId: "run-a", surface: "outcomes"},
    {kind: "operator-decision", entityId: "decision-a", runId: "run-a", surface: "outcomes"},
    {kind: "proof-bundle", entityId: "dossier-a", runId: "run-a", surface: "dossiers"},
    {kind: "finding", entityId: "finding-forged", runId: "run-a", surface: "artifacts"},
    {kind: "worker", entityId: "worker-a", runId: "run-a", surface: "outcomes"},
    {kind: "finding", entityId: "finding-no-run", runId: "", surface: "outcomes"},
    {kind: "finding", entityId: `finding-<img src=x onerror="bad">`, runId: "run-a", surface: "outcomes"},
  ]},
  allowedActions: ["pause", "stop"],
};

const links = Campaigns.typedLinks(campaign);
assert.strictEqual(links.length, 6);
assert.deepStrictEqual(links.slice(0, 5).map(value => [value.kind, value.surface]), [
  ["evidence", "artifacts"], ["hypothesis", "outcomes"], ["finding", "outcomes"],
  ["operator-decision", "outcomes"], ["proof-bundle", "dossiers"],
]);
const detail = Campaigns.renderDetail(campaign);
for (const marker of [
  'data-campaign-link="artifacts"', 'data-campaign-link="outcomes"',
  'data-campaign-link="dossiers"', 'data-campaign-kind="operator-decision"',
  'data-campaign-ref="finding-a"', 'data-campaign-run="run-a"',
  'data-campaign-copy="proof-bundle:dossier-a"',
]) assert.ok(detail.includes(marker), marker);
assert.ok(!detail.includes("finding-forged"));
assert.ok(!detail.includes("worker-a"));
assert.ok(!detail.includes("<img"));
assert.ok(detail.includes("&lt;img"));

const empty = Campaigns.renderDetail({...campaign, typedLinks: {references: []}});
assert.match(empty, /No resolved records/);

console.log("mission campaign links adversarial: ok");
