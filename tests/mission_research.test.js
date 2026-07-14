const test = require("node:test");
const assert = require("node:assert/strict");
const MissionResearch = require("../src/rekit_factory/ui/mission-research.js");

function snapshot() {
  return {
    hypothesisState: {
      hypotheses: [{id: "hyp-a", claim: "Parser length controls allocation", status: "testing", confidence: .72, expectedObservation: "allocation follows length", falsifier: "allocation is constant", stopCondition: {max_attempts: 2, max_cost_units: 30}, references: [{kind: "evidence", id: "artifact-a"}]}],
      tests: [{id: "test-a", hypothesisId: "hyp-a", objective: "Vary length", status: "testing", priority: 410, attempts: 1, expected_observation: "allocation changes", falsifying_observation: "no change"}],
      observations: [{id: "obs-a", hypothesisId: "hyp-a", outcome: "supported", references: [{kind: "evidence", id: "artifact-b"}]}],
    },
    findingState: {
      findings: [{id: "finding-a", lifecycleStatus: "operator-accepted", status: "reproduced", impactClaim: "Length can exhaust memory", findingType: "defect", consequence: "high", confidence: .91, knownUncertainty: "Architecture variance", proofPolicy: {successful_clean_reproductions: 2}, references: [{kind: "evidence", id: "artifact-c"}], operatorDecision: {decision: "accepted", rationale: "Proof reviewed"}}],
      attempts: [{id: "attempt-a", findingId: "finding-a", outcome: "success"}, {id: "attempt-b", findingId: "finding-a", outcome: "negative"}],
    },
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
