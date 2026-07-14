const test = require("node:test");
const assert = require("node:assert/strict");
const MissionResearch = require("../src/rekit_factory/ui/mission-research.js");

function snapshot() {
  return {
    meta: {projectId: "project-a"},
    hypothesisState: {
      hypotheses: [{id: "hyp-a", claim: "Parser length controls allocation", status: "testing", confidence: .72, expectedObservation: "allocation follows length", falsifier: "allocation is constant", stopCondition: {max_attempts: 2, max_cost_units: 30}, references: [{kind: "evidence", id: "artifact-a"}]}],
      tests: [{id: "test-a", hypothesisId: "hyp-a", objective: "Vary length", status: "testing", priority: 410, attempts: 1, expected_observation: "allocation changes", falsifying_observation: "no change"}],
      observations: [{id: "obs-a", hypothesisId: "hyp-a", outcome: "supported", references: [{kind: "evidence", id: "artifact-b"}]}],
    },
    findingState: {
      findings: [{id: "finding-a", lifecycleStatus: "operator-accepted", status: "reproduced", impactClaim: "Length can exhaust memory", findingType: "defect", consequence: "high", confidence: .91, knownUncertainty: "Architecture variance", proofPolicy: {successful_clean_reproductions: 2, require_independent_worker: true, require_independent_session: true, require_clean_environment: true}, recipe: {clean_environment_requirements: ["fresh workspace"]}, references: [{kind: "evidence", id: "artifact-c"}], operatorDecision: {decision: "accepted", rationale: "Proof reviewed"}}],
      attempts: [{id: "attempt-a", findingId: "finding-a", outcome: "success", workerId: "worker-a", sessionId: "session-a", modelProfile: "profile-a", environment: {id: "clean-linux", clean: true, platform: "linux", architecture: "x86_64", isolation: "container"}, observations: ["bounded observation"], environmentalDifferences: [], references: [{kind: "evidence", id: "artifact-c"}]}, {id: "attempt-b", findingId: "finding-a", outcome: "negative"}],
    },
    dossiers: [{id: "dossier-a", findingId: "finding-a", manifestSha256: "a".repeat(64), findingStateSha256: "b".repeat(64), verdict: "accepted", findingStatus: "reproduced", artifactIds: {"proof-bundle": "bundle-a", "proof-report": "report-a"}, verificationStatus: "verified", verified: true}],
    artifacts: [{id: "bundle-a", kind: "proof-bundle", sha256: "c".repeat(64), size_bytes: 512, path: "/private/never-render"}, {id: "report-a", kind: "proof-report", sha256: "d".repeat(64), size_bytes: 256}],
    evidenceRecords: [{artifactId: "artifact-c", kind: "reproduction-output", state: "active", originalSha256: "e".repeat(64), displaySha256: "f".repeat(64), redacted: true, truncated: false, quarantineLabels: []}],
    outcomeProjection: {degraded: false},
  };
}

test("joins canonical tests, observations, and attempts without transcript state", () => {
  const value = MissionResearch.model(snapshot());
  assert.equal(value.hypotheses[0].lane, "testing");
  assert.deepEqual(value.hypotheses[0].tests.map(item => item.id), ["test-a"]);
  assert.deepEqual(value.hypotheses[0].observations.map(item => item.id), ["obs-a"]);
  assert.equal(value.findings[0].lane, "operator-accepted");
  assert.equal(value.findings[0].attempts.length, 2);
});

test("renders distinct hypothesis and proof lanes with exact evidence links", () => {
  const html = MissionResearch.render(snapshot());
  for (const marker of ["Hypothesis board", "Finding & proof board", "testing", "operator accepted", "1/2", "data-research-ref-id=\"artifact-a\"", "data-research-outcome=\"finding-a\""]) assert.match(html, new RegExp(marker));
  assert.doesNotMatch(html, /raw transcript|model message/);
});

test("hostile canonical display text is escaped and empty/degraded-compatible", () => {
  const hostile = snapshot(); hostile.hypothesisState.hypotheses[0].claim = "<img src=x onerror=alert(1)>";
  const html = MissionResearch.render(hostile);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(MissionResearch.render({}), /No hypotheses/);
});

test("renders unified canonical proof dossier attempts prerequisites redaction and hashes", () => {
  const html = MissionResearch.render(snapshot());
  for (const marker of ["Unified proof detail", "Proof prerequisites", "independent worker", "fresh workspace", "Reproduction & validation attempts", "clean-linux", "Proof manifests & dossiers", "hash verified", "manifest aaaa", "Artifact redaction & hashes", "redacted display projection", "original eeee", "display ffff", 'data-research-ref-kind="proof-bundle"', 'data-research-ref-id="dossier-a"']) assert.match(html, new RegExp(marker));
  assert.doesNotMatch(html, /\/private\/never-render/);
});

test("degraded projection withholds dossier joins and hostile references fail closed", () => {
  const value = snapshot();
  value.outcomeProjection.degraded = true;
  value.findingState.findings[0].references.push({kind: "evidence", id: "<img onerror=alert(1)>"}, {kind: "credential", id: "secret"});
  const model = MissionResearch.model(value), html = MissionResearch.render(value);
  assert.equal(model.findings[0].dossiers.length, 0);
  assert.match(html, /Proof dossier joins are withheld/);
  assert.doesNotMatch(html, /dossier-a|onerror|credential:secret/);
});

test("large proof histories are bounded before rendering", () => {
  const value = snapshot();
  value.findingState.attempts = Array.from({length: 1000}, (_, index) => ({id: `attempt-${index}`, findingId: "finding-a", outcome: "negative"}));
  value.evidenceRecords = Array.from({length: 1000}, (_, index) => ({artifactId: `artifact-${index}`, kind: "output", state: "active", originalSha256: "a".repeat(64), displaySha256: "b".repeat(64), redacted: false, truncated: false}));
  value.findingState.findings[0].references = value.evidenceRecords.map(item => ({kind: "evidence", id: item.artifactId}));
  const model = MissionResearch.model(value);
  assert.equal(model.findings[0].attempts.length, 32);
  assert.equal(model.findings[0].proofEvidence.length, 32);
});

test("reconnect renders are deterministic, non-mutating, and retain native keyboard controls", () => {
  const value = snapshot(), before = JSON.stringify(value);
  const first = MissionResearch.render(value), second = MissionResearch.render(value);
  assert.equal(first, second);
  assert.equal(JSON.stringify(value), before);
  assert.match(first, /<details class="research-proof-detail"><summary>/);
  assert.match(first, /aria-label="operator accepted"/);
  for (const button of first.matchAll(/<button\b[^>]*>/g)) assert.match(button[0], /type="button"/);
});

test("large multi-finding histories remain per-finding bounded and omit private evidence fields", () => {
  const value = snapshot();
  value.findingState.findings = Array.from({length: 256}, (_, index) => ({
    ...value.findingState.findings[0], id: `finding-${index}`,
    references: Array.from({length: 64}, (__, ref) => ({kind: "evidence", id: `artifact-${ref}`})),
  }));
  value.findingState.attempts = Array.from({length: 512}, (_, index) => ({
    id: `attempt-${index}`, findingId: `finding-${index % 256}`, outcome: "negative",
    references: [{kind: "evidence", id: `artifact-${index % 64}`}],
  }));
  value.evidenceRecords = Array.from({length: 512}, (_, index) => ({
    artifactId: `artifact-${index}`, kind: "output", state: "active",
    originalSha256: "a".repeat(64), displaySha256: "b".repeat(64),
    rawPath: `/secret/raw/${index}`, displayPath: `/secret/display/${index}`, rawBytes: "PRIVATE_BYTES",
  }));
  const model = MissionResearch.model(value), html = MissionResearch.render(value);
  assert.equal(model.findings.length, 256);
  assert.ok(model.findings.every(finding => finding.attempts.length <= 32 && finding.proofEvidence.length <= 32));
  assert.doesNotMatch(html, /\/secret\/|PRIVATE_BYTES|rawPath|displayPath/);
});

test("renders only exact server-published finding authority", () => {
  const value = snapshot();
  value.memoryAuthority = {schemaVersion: 1, projectId: "project-a", revision: 12, degraded: false, operations: [
    {action: "finding-accept", entityType: "finding", entityId: "finding-a", expectedEntitySha256: "a".repeat(64)},
    {action: "finding-reject", entityType: "finding", entityId: "finding-a", expectedEntitySha256: "a".repeat(64)},
    {action: "finding-accept", entityType: "finding", entityId: "other", expectedEntitySha256: "b".repeat(64)},
  ]};
  const html = MissionResearch.render(value);
  assert.match(html, /data-memory-operation="finding-accept"/);
  assert.match(html, /data-memory-operation="finding-reject"/);
  assert.match(html, /data-memory-revision="12"/);
  assert.match(html, /data-memory-project="project-a"/);
  assert.match(html, new RegExp(`data-memory-digest="${"a".repeat(64)}"`));
  value.memoryAuthority.operations[0].expectedEntitySha256 = "forged";
  value.memoryAuthority.operations[1].entityType = "workstream";
  assert.doesNotMatch(MissionResearch.render(value), /data-memory-operation=/);
  value.memoryAuthority.degraded = true;
  assert.equal(MissionResearch.authorityOperations(value).length, 0);
});
