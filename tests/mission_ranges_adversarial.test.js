"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const Ranges = require("../src/rekit_factory/ui/mission-ranges.js");

function record() {
  return {
    schemaVersion: 1, rangeId: "range-1", status: "ready", revision: 3, generation: 1,
    updatedAt: "2026-07-13T12:00:00Z", expiresAt: "2026-07-13T13:00:00Z",
    specSha256: "a".repeat(64), scopeSha256: "b".repeat(64), templateSha256: "c".repeat(64),
    topologySha256: "d".repeat(64), executionSha256: "e".repeat(64),
    benchmarkComparisonKey: `sha256:${"f".repeat(64)}`,
    nodes: [{nodeId: "analyzer", platform: "linux", architecture: "x86_64",
      imageSha256: "1".repeat(64), environmentSha256: "2".repeat(64), toolCount: 2}],
    attachments: [{auditId: "range-attach:a1", nodeId: "analyzer", action: "observe-console",
      requestedBy: "operator-1", disposition: "allowed", reasonCode: "authorized",
      createdAt: "2026-07-13T12:00:00Z", expiresAt: "2026-07-13T12:05:00Z"}],
    failure: null,
  };
}

test("renders bounded health and exact read-only attachment audit", () => {
  const card = Ranges.renderBoard([record()]);
  const detail = Ranges.renderDetail(record());
  assert.match(card, /range-1/);
  assert.match(card, /runtime identity bound/);
  assert.match(detail, /observe console/);
  assert.match(detail, /operator-1/);
  assert.match(detail, /comparison sha256:/);
});

test("ignores hostile provider fields and escapes every rendered stable value", () => {
  const hostile = record();
  hostile.providerCredential = "TOKEN=never-render";
  hostile.hostPath = "/Users/private/target";
  hostile.nodes[0].providerHandle = "provider-secret";
  hostile.attachments[0].requestedBy = "<img src=x onerror=alert(1)>";
  const detail = Ranges.renderDetail(hostile);
  assert.doesNotMatch(detail, /TOKEN|Users|provider-secret|onerror/);
  assert.doesNotMatch(detail, /observe console/); // malformed audit is dropped as a unit
});

test("fails closed for forged top-level identity and bounds hostile collections", () => {
  const forged = record();
  forged.specSha256 = "not-a-digest";
  assert.match(Ranges.renderDetail(forged), /failed bounded client validation/);
  const many = Array.from({length: 100}, (_, index) => ({...record(), rangeId: `range-${index}`}));
  const rendered = Ranges.renderBoard(many);
  assert.equal((rendered.match(/class="range-card/g) || []).length, 64);
});
