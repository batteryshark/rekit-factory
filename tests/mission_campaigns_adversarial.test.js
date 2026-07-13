"use strict";

const assert = require("assert");
const Campaigns = require("../src/rekit_factory/ui/mission-campaigns.js");

function fixture(overrides = {}) {
  return {
    schemaVersion: 1,
    campaignId: "campaign-safe",
    projectId: "project-safe",
    scope: {scopeId: "scope-safe", revision: 2, digest: "a".repeat(64)},
    status: "running",
    revision: 7,
    currentEpoch: {epochId: "epoch-safe", ordinal: 3, workIds: ["work-a", "work-b"]},
    latestCheckpointId: "checkpoint-safe",
    cumulativeUsage: {workItems: 4, costUnits: 10},
    budget: {
      cumulative: {
        workItems: {value: 12, unit: "items"},
        costUnits: {value: 40, unit: "cost-units"},
      },
      remaining: {workItems: 8, costUnits: 30},
    },
    recommendation: null,
    recommendationDisposition: null,
    terminal: null,
    handoff: {
      reasonCode: "running", checkpointId: "checkpoint-safe",
      evidenceIds: ["evidence-safe"], factoryRunIds: ["run-safe"],
      evidenceCount: 1, factoryRunCount: 1, truncated: false,
    },
    allowedActions: ["pause", "stop"],
    ...overrides,
  };
}

// Needs-you is an exact canonical ask, not a guess from generic waiting state.
assert.strictEqual(Campaigns.needsAction(fixture({status: "waiting"})), false);
assert.strictEqual(Campaigns.needsAction(fixture({
  status: "waiting",
  recommendation: {action: "backoff", reasonCode: "retry-later"},
  recommendationDisposition: "pending",
})), false);
const needs = fixture({
  status: "waiting",
  recommendation: {
    action: "ask-operator", reasonCode: "no-progress",
    recommendationId: "recommendation-safe",
  },
  recommendationDisposition: "pending",
});
assert.strictEqual(Campaigns.needsAction(needs), true);
assert.match(Campaigns.renderCard(needs), /needs-action/);
assert.match(Campaigns.renderCard(needs), /NEEDS YOU/);
assert.strictEqual(Campaigns.needsAction({...needs, recommendationDisposition: "applied"}), false);

// Server-provided actions are allowlisted; injected browser fields cannot forge authority.
assert.deepStrictEqual(
  Campaigns.canonicalActions(fixture({allowedActions: ["pause", "expand-scope", "stop"]})),
  ["pause", "stop"],
);
assert.deepStrictEqual(Campaigns.canonicalActions({allowedActions: "pause"}), []);
assert.deepStrictEqual(Campaigns.canonicalActions(fixture({health: {degraded: true, problemCount: 2}})), []);

// Empty/degraded projections remain renderable and do not invent budget or checkpoint state.
assert.doesNotThrow(() => Campaigns.renderCard({}));
const degraded = Campaigns.renderDetail({campaignId: "campaign-degraded", status: "future"});
assert.match(degraded, /Projection unavailable|Budget projection unavailable/);
assert.match(degraded, /No bounded links/);
assert.strictEqual(Campaigns.statusTone("future-status"), "degraded");
assert.match(Campaigns.renderCard(fixture({health: {degraded: true, problemCount: 2}})), /degraded · 2/);

// The helper ignores non-contract goal/transcript/path fields and HTML-escapes every canonical
// value. This protects cards even when a degraded or stale response carries hostile extras.
const hostile = fixture({
  goal: "/Users/private SECRET_TOKEN raw transcript",
  campaignId: `campaign-<img src=x onerror="alert(1)">`,
  latestCheckpointId: `<script>alert(1)</script>`,
  handoff: {
    reasonCode: `<svg onload=alert(1)>`, evidenceIds: ["evidence-<bad>"],
    factoryRunIds: ["run-\"bad\""], evidenceCount: 1, factoryRunCount: 1,
  },
});
const hostileCard = Campaigns.renderCard(hostile);
const hostileDetail = Campaigns.renderDetail(hostile);
for (const forbidden of ["/Users/private", "SECRET_TOKEN", "raw transcript", "<script>", "<img", "<svg"])
  assert.ok(!hostileCard.includes(forbidden) && !hostileDetail.includes(forbidden), forbidden);
assert.match(hostileDetail, /&lt;script&gt;/);
assert.match(hostileDetail, /class="campaign-reference"/);
assert.ok(!hostileDetail.includes('data-campaign-link="artifacts"'));
assert.match(hostileDetail, /data-campaign-link="activity"/);

// A maximum bounded handoff remains dense but complete, with stable deep-link controls.
const large = fixture({handoff: {
  reasonCode: "waiting", evidenceCount: 500, factoryRunCount: 500, truncated: true,
  evidenceIds: Array.from({length: 32}, (_, index) => `evidence-${index}`),
  factoryRunIds: Array.from({length: 32}, (_, index) => `run-${index}`),
}});
const largeDetail = Campaigns.renderDetail(large);
assert.strictEqual((largeDetail.match(/class="campaign-reference"/g) || []).length, 32);
assert.strictEqual((largeDetail.match(/class="campaign-link"/g) || []).length, 32);
assert.match(largeDetail, /500/);

for (const [status, expected] of Object.entries({
  running: "productive", waiting: "waiting", exhausted: "exhausted", blocked: "blocked",
  "policy-stopped": "policy-stopped", failed: "failed", completed: "successful",
})) assert.strictEqual(Campaigns.statusTone(status), expected);

console.log("mission campaigns adversarial: ok");
