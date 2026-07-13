"use strict";

const assert = require("node:assert/strict");
const {webcrypto} = require("node:crypto");
const {
  canonicalLink, canonicalSemanticText, createSemanticTracker, projectionView, semanticSha256,
} = require("../src/rekit_factory/ui/mission-outcomes.js");

const empty = {
  schemaVersion: 1,
  vocabularyVersion: "factory-outcomes/v1",
  facets: [],
  authorities: {},
  entities: [],
  diagnostics: [],
  degraded: false,
  sourceWatermarks: {factoryEventRowid: 9},
  consistency: {mode: "full-fold"},
};
const expectedText = "{\"domain\":\"factory-outcomes/semantic-sha256/v1\",\"projection\":{\"authorities\":{},\"consistency\":{\"mode\":\"full-fold\"},\"degraded\":false,\"diagnostics\":[],\"entities\":[],\"facets\":[],\"schemaVersion\":1,\"vocabularyVersion\":\"factory-outcomes/v1\"}}";
const expectedDigest = "c422738c6fc7f207cb5ea7f296181c6a777c06c286eb0c13d93fc98b7af4f444";

const facet = (state, owner, {known = true, terminal = false, rawState = state} = {}) => ({
  rawState, state, known, terminal, owner,
});
const na = owner => facet("not-applicable", owner, {terminal: true, rawState: null});
const entity = (entityType, entityId, values = {}, parent = null, diagnostics = []) => ({
  entityType, entityId, parent,
  facets: {
    execution: values.execution || na("factory-scheduler"),
    completion: values.completion || na("factory-scheduler"),
    disposition: values.disposition || na("factory-scheduler"),
    validation: values.validation || na("validator-policy"),
    acceptance: values.acceptance || na("operator"),
    publication: values.publication || na("factory-dossier-publisher"),
  },
  diagnostics,
});

(async () => {
  assert.equal(canonicalSemanticText(empty), expectedText, "browser canonicalization matches the public Python byte domain");
  assert.equal(await semanticSha256(empty, webcrypto.subtle), expectedDigest);
  const watermarkMoved = {...empty, sourceWatermarks: {factoryEventRowid: 900}};
  assert.equal(await semanticSha256(watermarkMoved, webcrypto.subtle), expectedDigest);

  const entities = [
    entity("run", "run-1", {
      execution: facet("active", "factory-scheduler"),
      completion: facet("incomplete", "factory-scheduler"),
      disposition: facet("deferred", "factory-scheduler"),
    }),
    entity("worker", "worker-terminal", {
      execution: facet("terminal", "factory-scheduler", {terminal: true, rawState: "done"}),
      completion: facet("completed", "factory-scheduler", {terminal: true, rawState: "done"}),
      disposition: facet("successful", "factory-scheduler", {terminal: true, rawState: "done"}),
    }, {entityType: "run", entityId: "run-1"}),
    entity("finding", "finding-unknown", {
      validation: facet("unknown", "validator-policy", {known: false, rawState: "future-state"}),
      acceptance: facet("accepted", "operator", {terminal: true}),
      publication: facet("published", "factory-dossier-publisher", {terminal: true}),
    }, {entityType: "run", entityId: "missing-run"}, [{code: "dangling-parent"}]),
    entity("proof-bundle", "proof-1", {
      validation: facet("verified", "offline-proof-verifier", {terminal: true}),
      publication: facet("published", "factory-dossier-publisher", {terminal: true}),
    }, {entityType: "finding", entityId: "finding-unknown"}),
    entity("operator-decision", "decision-1", {
      disposition: facet("needs-review", "operator"), acceptance: facet("undecided", "operator"),
    }, {entityType: "run", entityId: "run-1"}),
  ];
  const projected = {...empty, entities, degraded: true, diagnostics: [{code: "dangling-parent"}]};
  const all = projectionView(projected);
  assert.deepEqual(all.counts, {
    total: 5, shown: 5, terminal: 3, unknown: 1, degraded: 1,
    types: {run: 1, worker: 1, finding: 1, "proof-bundle": 1, "operator-decision": 1},
  });
  assert.deepEqual(projectionView(projected, {type: "finding"}).entities.map(item => item.entityId), ["finding-unknown"]);
  assert.deepEqual(projectionView(projected, {state: "accepted"}).entities.map(item => item.entityId), ["finding-unknown"]);
  assert.deepEqual(projectionView(projected, {owner: "offline-proof-verifier"}).entities.map(item => item.entityId), ["proof-1"]);
  assert.deepEqual(projectionView(projected, {terminal: "nonterminal"}).entities.map(item => item.entityId), ["run-1", "decision-1"], "not-applicable terminal facets do not promote an entity");
  assert.deepEqual(projectionView(projected, {query: "missing-run"}).entities.map(item => item.entityId), ["finding-unknown"]);
  assert.deepEqual(projectionView(projected, {query: "future-state"}).entities.map(item => item.entityId), ["finding-unknown"]);

  const large = {...empty, entities: Array.from({length: 250}, (_, index) => entity("work-item", `work-${String(index).padStart(3, "0")}`, {execution: facet(index % 2 ? "active" : "terminal", "muster", {terminal: index % 2 === 0})}))};
  assert.equal(projectionView(large).counts.total, 250);
  assert.equal(projectionView(large, {query: "work-249"}).counts.shown, 1);

  assert.deepEqual(canonicalLink(entities[0]), {tab: "activity", label: "Open activity"});
  assert.deepEqual(canonicalLink(entities[3]), {tab: "dossiers", label: "Open dossiers"});
  assert.deepEqual(canonicalLink(entities[4]), {tab: "decisions", label: "Open decisions"});
  assert.equal(canonicalLink(entity("hypothesis", "hypothesis-1")), null, "no report/evidence link is invented");

  const tracker = createSemanticTracker();
  const first = {...empty, semanticSha256: expectedDigest};
  assert.equal((await tracker.accept(first, () => Promise.resolve(expectedDigest))).action, "render");
  assert.equal((await tracker.accept({...first, sourceWatermarks: {factoryEventRowid: 10}}, () => Promise.resolve(expectedDigest))).action, "retain", "watermark-only updates retain DOM and focus state");
  const changedDigest = "a".repeat(64), changed = {...first, degraded: true, semanticSha256: changedDigest};
  assert.equal((await tracker.accept(changed, () => Promise.resolve(changedDigest))).action, "render");
  assert.equal((await tracker.accept(changed, () => Promise.resolve(changedDigest))).action, "retain", "one semantic identity renders exactly once");

  const racing = createSemanticTracker();
  let releaseOld;
  const old = racing.accept(first, () => new Promise(resolve => { releaseOld = resolve; }));
  const latest = await racing.accept(changed, () => Promise.resolve(changedDigest));
  releaseOld(expectedDigest);
  assert.equal(latest.action, "render");
  assert.equal((await old).action, "stale", "late verification cannot replace a newer semantic projection");
  assert.equal((await tracker.accept({...empty})).integrity, "legacy", "older snapshots remain renderable");
  assert.equal((await tracker.accept({...empty, semanticSha256: "b".repeat(64)}, () => Promise.resolve("c".repeat(64)))).integrity, "mismatch");
  assert.equal((await tracker.accept({...empty, semanticSha256: "d".repeat(64)}, () => Promise.reject(new Error("no crypto")))).integrity, "unavailable");

  console.log("mission outcomes behavior: ok");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
