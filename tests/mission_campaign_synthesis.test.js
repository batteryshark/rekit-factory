const test = require("node:test");
const assert = require("node:assert/strict");
const Campaigns = require("../src/rekit_factory/ui/mission-campaigns.js");

function campaign(overrides = {}) {
  return {
    campaignId: "campaign-a", status: "running", recommendationDisposition: "pending",
    recommendation: {action: "ask-operator", reasonCode: "scope-review"},
    health: {degraded: false, current: {sequence: 4, phase: "validation",
      coverageBasisPoints: 6250, epochNovelProgress: 2, cumulativeNovelProgress: 7,
      noProgressCount: 0, retryCount: 1, elapsedWallSeconds: 91,
      nextCheckpointExpectedWallSeconds: 120}},
    budget: {remaining: {workItems: 3, costUnits: 12, wallSeconds: 29},
      cumulative: {workItems: {value: 10, unit: "items"}, costUnits: {value: 40, unit: "units"}, wallSeconds: {value: 120, unit: "seconds"}}},
    cumulativeUsage: {workItems: 7, costUnits: 28, wallSeconds: 91},
    changeRequests: [],
    typedLinks: {references: [
      {kind: "proof-bundle", entityId: "dossier-a", runId: "run-a", surface: "dossiers"},
      {kind: "hypothesis", entityId: "hypothesis-a", runId: "run-a", surface: "outcomes"},
    ], strongestReproducedResult: {kind: "proof-bundle", entityId: "dossier-a", findingId: "finding-a", runId: "run-a", surface: "dossiers", basis: "operator-accepted-published-proof"}, currentResearchFocus: {runId: "run-a", hypothesisId: "hypothesis-a", testId: "test-a", surface: "outcomes", phase: "testing", hypothesisClaim: "The parser length field controls allocation size.", objective: "Vary only the length field.", textTruncated: false}},
    ...overrides,
  };
}

test("one-screen synthesis prioritizes needs-action and preserves exact proof deep link", () => {
  const quiet = campaign({campaignId: "campaign-b", recommendationDisposition: "applied", recommendation: {action: "continue", reasonCode: "progress"}});
  const value = Campaigns.synthesis([quiet, campaign()]);
  assert.equal(value[0].campaignId, "campaign-a");
  assert.equal(value[0].needsAction, true);
  assert.equal(value[0].progress.coverage, "62.50%");
  assert.equal(value[0].strongest.entityId, "dossier-a");
  assert.equal(value[0].focus.objective, "Vary only the length field.");
  const html = Campaigns.renderSynthesis([quiet, campaign()]);
  for (const marker of ["NEEDS YOU", "62.50%", "dossier-a", "Vary only the length field", "parser length field", 'data-campaign-link="dossiers"', 'data-campaign-ref="hypothesis-a"', 'data-campaign-run="run-a"']) assert.match(html, new RegExp(marker));
});

test("forged strongest result fails closed and hostile fields are escaped", () => {
  const hostile = campaign({campaignId: "campaign-safe", recommendation: {action: "ask-operator", reasonCode: '<img src=x onerror="bad">'}, typedLinks: {references: [{kind: "hypothesis", entityId: "hypothesis-a", runId: "run-a", surface: "outcomes"}], strongestReproducedResult: {kind: "proof-bundle", entityId: "forged", findingId: "finding", runId: "run", surface: "dossiers", basis: "operator-accepted-published-proof"}, currentResearchFocus: {runId: "run-a", hypothesisId: "hypothesis-a", testId: "test-a", surface: "outcomes", phase: "testing", hypothesisClaim: '<svg onload="bad">', objective: "Probe <script> behavior", textTruncated: false}}});
  const html = Campaigns.renderSynthesis([hostile]);
  assert.match(html, /none qualified/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;svg/);
  assert.match(html, /Probe &lt;script&gt; behavior/);
});

test("empty and degraded projections never invent progress, budget, action, or proof", () => {
  assert.match(Campaigns.renderSynthesis([]), /No campaign synthesis yet/);
  const degraded = Campaigns.synthesis([campaign({health: {degraded: true}, recommendation: {action: "ask-operator", reasonCode: "private"}})])[0];
  assert.equal(degraded.needsAction, false);
  assert.deepEqual(degraded.progress, {});
  assert.deepEqual(degraded.budgets, []);
  assert.equal(degraded.strongest, null);
  assert.equal(degraded.focus, null);
  assert.match(Campaigns.renderSynthesis([campaign({health: {degraded: true}})]), /canonical health unavailable/);
});
