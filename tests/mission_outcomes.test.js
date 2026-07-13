"use strict";

const assert = require("node:assert/strict");
const {webcrypto} = require("node:crypto");
const {
  canonicalLink, canonicalSemanticText, createGenerationGate, createSemanticTracker,
  decodeSemanticEnvelope, eventStreamUrl, isCurrentEventStream, latestEventId,
  projectionView, reportFacets, semanticSha256,
} = require("../src/rekit_factory/ui/mission-outcomes.js");

const empty = {
  schemaVersion: 1,
  vocabularyVersion: "factory-outcomes/v2",
  facets: ["execution", "completion", "disposition", "validation", "acceptance", "publication", "coverage", "archival"],
  authorities: {},
  entities: [],
  diagnostics: [],
  degraded: false,
  sourceWatermarks: {factoryEventRowid: 9},
  consistency: {mode: "full-fold"},
};
const expectedText = "{\"domain\":\"factory-outcomes/semantic-sha256/v1\",\"projection\":{\"authorities\":{},\"consistency\":{\"mode\":\"full-fold\"},\"degraded\":false,\"diagnostics\":[],\"entities\":[],\"facets\":[\"execution\",\"completion\",\"disposition\",\"validation\",\"acceptance\",\"publication\",\"coverage\",\"archival\"],\"schemaVersion\":1,\"vocabularyVersion\":\"factory-outcomes/v2\"}}";
const expectedDigest = "fa9c207e4467b35c2cf5971cfe8544218e625992b5e9846ba8f193ac624f2d7a";
const expectedBase64 = "eyJkb21haW4iOiJmYWN0b3J5LW91dGNvbWVzL3NlbWFudGljLXNoYTI1Ni92MSIsInByb2plY3Rpb24iOnsiYXV0aG9yaXRpZXMiOnt9LCJjb25zaXN0ZW5jeSI6eyJtb2RlIjoiZnVsbC1mb2xkIn0sImRlZ3JhZGVkIjpmYWxzZSwiZGlhZ25vc3RpY3MiOltdLCJlbnRpdGllcyI6W10sImZhY2V0cyI6WyJleGVjdXRpb24iLCJjb21wbGV0aW9uIiwiZGlzcG9zaXRpb24iLCJ2YWxpZGF0aW9uIiwiYWNjZXB0YW5jZSIsInB1YmxpY2F0aW9uIiwiY292ZXJhZ2UiLCJhcmNoaXZhbCJdLCJzY2hlbWFWZXJzaW9uIjoxLCJ2b2NhYnVsYXJ5VmVyc2lvbiI6ImZhY3Rvcnktb3V0Y29tZXMvdjIifX0=";
// Generated once from Python's exact ensure_ascii=False, sort_keys=True canonical bytes.
// It intentionally contains 2.0, 1e-07, integer-like keys, BMP, and astral Unicode keys.
const vectorBase64 = "eyJkb21haW4iOiJmYWN0b3J5LW91dGNvbWVzL3NlbWFudGljLXNoYTI1Ni92MSIsInByb2plY3Rpb24iOnsiYXV0aG9yaXRpZXMiOnt9LCJjb25zaXN0ZW5jeSI6eyJtb2RlIjoiZnVsbC1mb2xkIn0sImRlZ3JhZGVkIjp0cnVlLCJkaWFnbm9zdGljcyI6W3siY29kZSI6InZlY3RvciIsInJhdyI6eyIxMCI6MSwiMiI6Mi4wLCJmbG9hdCI6MWUtMDcsInVuaWNvZGUiOnsi7oCAIjoiYm1wIiwi8JCAgCI6ImFzdHJhbCJ9fX1dLCJlbnRpdGllcyI6W10sImZhY2V0cyI6WyJleGVjdXRpb24iLCJjb21wbGV0aW9uIiwiZGlzcG9zaXRpb24iLCJ2YWxpZGF0aW9uIiwiYWNjZXB0YW5jZSIsInB1YmxpY2F0aW9uIiwiY292ZXJhZ2UiLCJhcmNoaXZhbCJdLCJzY2hlbWFWZXJzaW9uIjoxLCJ2b2NhYnVsYXJ5VmVyc2lvbiI6ImZhY3Rvcnktb3V0Y29tZXMvdjIifX0=";
const vectorDigest = "c729a9967a4f6a06cf1ec2ce1ed357d8e017f485a422d61a61b5011d5ba0ecbc";

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
    coverage: values.coverage || na("muster"),
    archival: values.archival || na("operator"),
  },
  diagnostics,
});

(async () => {
  assert.equal(canonicalSemanticText(empty), expectedText, "legacy text is deterministic locally");
  const first = {...empty, semanticCanonicalBase64: expectedBase64, semanticSha256: expectedDigest};
  const vector = {
    ...empty, entities: [{entityType: "outer-must-not-render"}],
    semanticCanonicalBase64: vectorBase64, semanticSha256: vectorDigest,
  };
  assert.equal(await semanticSha256(first, webcrypto.subtle), expectedDigest);
  assert.equal(await semanticSha256(vector, webcrypto.subtle), vectorDigest);
  const decodedVector = decodeSemanticEnvelope(vector);
  const exactVectorText = new TextDecoder().decode(decodedVector.bytes);
  assert.ok(exactVectorText.includes('\"10\":1,\"2\":2.0,\"float\":1e-07'));
  assert.ok(exactVectorText.includes('\"\":\"bmp\",\"𐀀\":\"astral\"'));
  assert.deepEqual(decodedVector.projection.entities, [], "rendering uses the signed envelope, never outer semantic fields");

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
    entity("campaign", "campaign-1", {
      execution: facet("active", "factory-scheduler", {rawState: "active"}),
      coverage: facet("covered", "muster", {terminal: true, rawState: "covered"}),
    }),
    entity("archive", "archive-1", {
      archival: facet("archived", "operator", {terminal: true, rawState: "archived"}),
    }, {entityType: "campaign", entityId: "campaign-1"}),
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
    total: 7, shown: 7, terminal: 5, unknown: 1, degraded: 1,
    types: {run: 1, worker: 1, finding: 1, campaign: 1, archive: 1, "proof-bundle": 1, "operator-decision": 1},
  });
  assert.deepEqual(projectionView(projected, {type: "finding"}).entities.map(item => item.entityId), ["finding-unknown"]);
  assert.deepEqual(projectionView(projected, {state: "accepted"}).entities.map(item => item.entityId), ["finding-unknown"]);
  assert.deepEqual(projectionView(projected, {owner: "offline-proof-verifier"}).entities.map(item => item.entityId), ["proof-1"]);
  assert.deepEqual(projectionView(projected, {terminal: "nonterminal"}).entities.map(item => item.entityId), ["run-1", "decision-1"], "not-applicable terminal facets do not promote an entity");
  assert.deepEqual(projectionView(projected, {state: "covered"}).entities.map(item => item.entityId), ["campaign-1"]);
  assert.deepEqual(projectionView(projected, {state: "archived"}).entities.map(item => item.entityId), ["archive-1"]);
  assert.equal(entities[3].facets.completion.state, "not-applicable", "coverage never derives campaign completion");
  assert.equal(entities[3].facets.disposition.state, "not-applicable", "coverage never derives campaign success");
  assert.equal(entities[4].facets.publication.state, "not-applicable", "archival is not report publication");
  assert.deepEqual(projectionView(projected, {query: "missing-run"}).entities.map(item => item.entityId), ["finding-unknown"]);
  assert.deepEqual(projectionView(projected, {query: "future-state"}).entities.map(item => item.entityId), ["finding-unknown"]);

  const large = {...empty, entities: Array.from({length: 250}, (_, index) => entity("work-item", `work-${String(index).padStart(3, "0")}`, {execution: facet(index % 2 ? "active" : "terminal", "muster", {terminal: index % 2 === 0})}))};
  assert.equal(projectionView(large).counts.total, 250);
  assert.equal(projectionView(large, {query: "work-249"}).counts.shown, 1);
  assert.deepEqual(projectionView(large, {query: "work-24", exactId: "work-249"}).entities.map(item => item.entityId), ["work-249"]);

  assert.deepEqual(canonicalLink(entities[0]), {tab: "activity", label: "Open activity"});
  assert.deepEqual(canonicalLink(entities[3]), {tab: "activity", label: "Open campaign activity"});
  assert.deepEqual(canonicalLink(entities[4]), {tab: "activity", label: "Open archive activity"});
  assert.deepEqual(canonicalLink(entities[5]), {tab: "dossiers", label: "Open dossiers"});
  assert.deepEqual(canonicalLink(entities[6]), {tab: "decisions", label: "Open decisions"});
  assert.equal(canonicalLink(entity("hypothesis", "hypothesis-1")), null, "no report/evidence link is invented");

  const tracker = createSemanticTracker();
  assert.equal((await tracker.accept(first, () => Promise.resolve(expectedDigest))).action, "render");
  assert.equal((await tracker.accept({...first, sourceWatermarks: {factoryEventRowid: 10}}, () => Promise.resolve(expectedDigest))).action, "retain", "watermark-only updates retain DOM and focus state");
  assert.equal((await tracker.accept(vector, () => Promise.resolve(vectorDigest))).action, "render");
  assert.equal((await tracker.accept(vector, () => Promise.resolve(vectorDigest))).action, "retain", "one semantic identity renders exactly once");

  const racing = createSemanticTracker();
  let releaseOld;
  const old = racing.accept(first, () => new Promise(resolve => { releaseOld = resolve; }));
  const latest = await racing.accept(vector, () => Promise.resolve(vectorDigest));
  releaseOld(expectedDigest);
  assert.equal(latest.action, "render");
  assert.equal((await old).action, "stale", "late verification cannot replace a newer semantic projection");

  const legacy = createSemanticTracker();
  assert.equal((await legacy.accept({...empty})).integrity, "legacy", "older snapshots remain renderable");
  assert.equal((await legacy.accept({...empty, sourceWatermarks: {factoryEventRowid: 999}})).action, "retain", "legacy watermark-only updates use local equality without claiming verification");
  const unavailable = createSemanticTracker();
  assert.equal((await unavailable.accept(first, () => Promise.reject(new Error("no crypto")))).integrity, "unavailable");
  assert.equal((await unavailable.accept({...first, sourceWatermarks: {factoryEventRowid: 12}}, () => Promise.reject(new Error("no crypto")))).action, "retain", "no-crypto mode preserves DOM using canonical Base64 equality");
  assert.equal((await createSemanticTracker().accept(first, () => Promise.resolve("c".repeat(64)))).integrity, "mismatch");
  assert.equal((await createSemanticTracker().accept({...first, semanticCanonicalBase64: "AAAA"})).integrity, "invalid-envelope");
  assert.equal((await createSemanticTracker().accept({...first, semanticCanonicalBase64: null})).integrity, "invalid-envelope");
  const invalidEnvelope = value => Buffer.from(JSON.stringify(value)).toString("base64");
  assert.equal((await createSemanticTracker().accept({...first, semanticCanonicalBase64: invalidEnvelope({domain: "wrong", projection: {}})})).integrity, "invalid-envelope");
  assert.equal((await createSemanticTracker().accept({...first, semanticCanonicalBase64: invalidEnvelope({domain: "factory-outcomes/semantic-sha256/v1", projection: [], extra: true})})).integrity, "invalid-envelope");

  const gate = createGenerationGate(), older = gate.begin(), newer = gate.begin();
  assert.equal(gate.isCurrent(older), false);
  assert.equal(gate.isCurrent(newer), true);
  gate.invalidate();
  assert.equal(gate.isCurrent(newer), false, "cross-run changes invalidate in-flight snapshot responses");

  const streamA = {}, streamB = {};
  assert.equal(isCurrentEventStream(streamA, streamA, "run-a", "run-a"), true);
  assert.equal(isCurrentEventStream(streamA, streamB, "run-a", "run-a"), false, "an old source cannot act on a replacement stream");
  assert.equal(isCurrentEventStream(streamA, streamA, "run-a", "run-b"), false, "an old source cannot invalidate a newly selected run");

  assert.equal(latestEventId([]), null);
  assert.equal(latestEventId([{id: "event-a"}, {id: "event-b"}]), "event-b");
  assert.equal(eventStreamUrl("run/a"), "/api/runs/run%2Fa/events");
  assert.equal(eventStreamUrl("run/a", "event ?#2"), "/api/runs/run%2Fa/events?after=event%20%3F%232");
  const report = entity("report", "work-a", {
    publication: facet("rendered", "factory-report-renderer", {terminal: true}),
  }, {entityType: "work-item", entityId: "work-a"});
  assert.deepEqual(reportFacets({facets: report.facets}).map(value => [value.name, value.state]), [
    ["execution", "not-applicable"], ["completion", "not-applicable"],
    ["disposition", "not-applicable"], ["validation", "not-applicable"],
    ["acceptance", "not-applicable"], ["publication", "rendered"],
    ["coverage", "not-applicable"], ["archival", "not-applicable"],
  ]);

  console.log("mission outcomes behavior: ok");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
