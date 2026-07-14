(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.MissionResearch = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const safe = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
  const percent = value => Number.isFinite(Number(value)) ? `${Math.round(Number(value) * 100)}%` : "—";
  const stable = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
  const digest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const refKinds = new Set(["evidence", "artifact", "hypothesis", "finding", "proof-bundle", "operator-decision"]);
  const refs = values => list(values).slice(0, 64).filter(value => value && refKinds.has(value.kind) && stable(value.id));
  const refButtons = values => refs(values).map(ref => `<button type="button" data-research-ref-kind="${safe(ref.kind)}" data-research-ref-id="${safe(ref.id)}">${safe(ref.kind)}:${safe(ref.id)}</button>`).join("");
  const hypothesisLane = status => status === "testing" ? "testing" : ["supported", "contradicted"].includes(status) ? "supported" : ["disproved", "retired"].includes(status) ? "disproved" : status === "reproduced" ? "reproduction" : "proposed";
  const findingLane = status => ["operator-accepted", "reproduced", "rejected", "withdrawn", "inconclusive"].includes(status) ? status : status || "candidate";
  const appendBounded = (index, key, value, ceiling) => {
    if (!stable(key)) return;
    const values = index.get(key) || [];
    if (values.length < ceiling) values.push(value);
    index.set(key, values);
  };

  function authorityOperations(snapshot) {
    const authority = object(snapshot?.memoryAuthority);
    if (authority.schemaVersion !== 1 || authority.degraded !== false
        || !stable(authority.projectId)
        || authority.projectId !== snapshot?.meta?.projectId
        || !Number.isInteger(authority.revision) || authority.revision < 0) return [];
    return list(authority.operations).slice(0, 64).flatMap(value => {
      const item = object(value);
      if (!stable(item.entityId) || !digest(item.expectedEntitySha256)
          || !["finding-accept", "finding-reject"].includes(item.action)
          || item.entityType !== "finding") return [];
      return [{action: item.action, entityId: item.entityId,
        expectedEntitySha256: item.expectedEntitySha256,
        expectedProjectId: authority.projectId,
        expectedRevision: authority.revision}];
    });
  }

  function model(snapshot) {
    const hypothesisState = object(snapshot?.hypothesisState), findingState = object(snapshot?.findingState);
    const degraded = snapshot?.outcomeProjection?.degraded === true;
    const tests = list(hypothesisState.tests).slice(0, 256), observations = list(hypothesisState.observations).slice(0, 512), attempts = list(findingState.attempts).slice(0, 512);
    const evidence = list(snapshot?.evidenceRecords).slice(0, 512).filter(item => stable(item?.artifactId));
    const artifacts = list(snapshot?.artifacts).slice(0, 512).filter(item => stable(item?.id));
    const dossiers = degraded ? [] : list(snapshot?.dossiers).slice(0, 128).filter(item => stable(item?.id) && stable(item?.findingId));
    const testsByHypothesis = new Map(), observationsByHypothesis = new Map(), attemptsByFinding = new Map(), attemptEvidenceByFinding = new Map(), dossiersByFinding = new Map();
    tests.forEach(item => appendBounded(testsByHypothesis, item?.hypothesisId, item, 256));
    observations.forEach(item => appendBounded(observationsByHypothesis, item?.hypothesisId, item, 512));
    attempts.forEach(item => {
      appendBounded(attemptsByFinding, item?.findingId, item, 32);
      if (!stable(item?.findingId)) return;
      const ids = attemptEvidenceByFinding.get(item.findingId) || new Set();
      refs(item.references).forEach(ref => ids.add(ref.id));
      attemptEvidenceByFinding.set(item.findingId, ids);
    });
    dossiers.forEach(item => appendBounded(dossiersByFinding, item?.findingId, item, 16));
    const hypotheses = list(hypothesisState.hypotheses).slice(0, 256).map(item => ({...item,
      lane: hypothesisLane(item.status), tests: testsByHypothesis.get(item.id) || [],
      observations: observationsByHypothesis.get(item.id) || [],
    }));
    const operations = authorityOperations(snapshot);
    const findings = list(findingState.findings).slice(0, 256).map(item => {
      const findingAttempts = attemptsByFinding.get(item.id) || [], dossiersForFinding = dossiersByFinding.get(item.id) || [];
      const evidenceIds = new Set(refs(item.references).map(ref => ref.id));
      (attemptEvidenceByFinding.get(item.id) || []).forEach(id => evidenceIds.add(id));
      const artifactIds = new Set(dossiersForFinding.flatMap(dossier => Object.values(object(dossier.artifactIds)).filter(stable)));
      return {...item,
        lane: findingLane(item.lifecycleStatus || item.status),
        attempts: findingAttempts,
        dossiers: dossiersForFinding,
        proofEvidence: evidence.filter(record => evidenceIds.has(record.artifactId)).slice(0, 32),
        proofArtifacts: artifacts.filter(artifact => artifactIds.has(artifact.id)).slice(0, 32),
        authorityOperations: operations.filter(operation => operation.entityId === item.id),
      };
    });
    return {hypotheses, findings, degraded};
  }

  function hypothesisCard(item) {
    const stop = object(item.stopCondition), evidence = item.observations.flatMap(value => refs(value.references));
    const tests = item.tests.map(test => `<li><b>${safe(test.objective || test.id)}</b><span>${safe(test.status)} · priority ${safe(test.priority ?? "—")} · ${safe(test.attempts ?? 0)} attempts</span><p>Expect: ${safe(test.expected_observation || test.expectedObservation || "—")}</p><p>Falsify: ${safe(test.falsifying_observation || test.falsifyingObservation || "—")}</p></li>`).join("");
    return `<article class="research-card hypothesis ${safe(item.lane)}" data-research-hypothesis="${safe(item.id)}"><header><span>${safe(item.id)}</span><b>${safe(item.status)}</b></header><h4>${safe(item.claim)}</h4><div class="research-meter"><i style="--confidence:${Math.max(0, Math.min(100, Number(item.confidence || 0) * 100))}%"></i><span>${percent(item.confidence)} confidence</span></div><dl><div><dt>expect</dt><dd>${safe(item.expectedObservation || "—")}</dd></div><div><dt>falsifier</dt><dd>${safe(item.falsifier || "—")}</dd></div><div><dt>stop</dt><dd>${safe(stop.max_attempts ?? stop.maxAttempts ?? "—")} attempts · ${safe(stop.max_cost_units ?? stop.maxCostUnits ?? "—")} cost</dd></div><div><dt>workstream</dt><dd>${safe(item.ownerWorkstream || "unassigned")}</dd></div></dl>${item.tests.length ? `<ul class="research-tests">${tests}</ul>` : ""}<footer>${refButtons([...refs(item.references), ...evidence]) || "<span>No evidence references projected</span>"}</footer></article>`;
  }

  function findingCard(item) {
    const successes = item.attempts.filter(value => value.outcome === "success").length, policy = object(item.proofPolicy), decision = object(item.operatorDecision);
    const controls = item.authorityOperations.map(operation => `<button class="btn ${operation.action === "finding-reject" ? "red" : "primary"}" type="button" data-memory-operation="${safe(operation.action)}" data-memory-entity="${safe(item.id)}" data-memory-project="${safe(operation.expectedProjectId)}" data-memory-revision="${operation.expectedRevision}" data-memory-digest="${safe(operation.expectedEntitySha256)}">${operation.action === "finding-accept" ? "Accept reproduced finding" : "Reject finding"}</button>`).join("");
    return `<article class="research-card finding ${safe(item.lane)}" data-research-finding="${safe(item.id)}"><header><span>${safe(item.findingType || "finding")} · ${safe(item.consequence || "unknown")}</span><b>${safe(item.lifecycleStatus || item.status)}</b></header><h4>${safe(item.impactClaim || item.id)}</h4><p class="research-uncertainty"><b>Known uncertainty</b>${safe(item.knownUncertainty || "None projected")}</p><div class="research-proof"><span><b>${successes}/${safe(policy.successful_clean_reproductions ?? policy.successfulCleanReproductions ?? "—")}</b> clean reproductions</span><span><b>${item.attempts.length}</b> attempts</span><span><b>${percent(item.confidence)}</b> confidence</span></div>${decision.decision ? `<div class="research-decision"><b>operator ${safe(decision.decision)}</b><span>${safe(decision.rationale || "")}</span></div>` : ""}${proofDetail(item)}${controls ? `<div class="research-authority-controls">${controls}</div>` : ""}<footer>${refButtons(item.references) || "<span>No evidence references projected</span>"}<button type="button" data-research-outcome="${safe(item.id)}">Canonical outcome →</button></footer></article>`;
  }

  function proofPolicy(policyValue, recipeValue) {
    const policy = object(policyValue), recipe = object(recipeValue), requirements = list(recipe.clean_environment_requirements ?? recipe.cleanEnvironmentRequirements).slice(0, 20);
    const rules = [
      ["clean reproductions", policy.successful_clean_reproductions ?? policy.successfulCleanReproductions],
      ["independent worker", policy.require_independent_worker ?? policy.requireIndependentWorker],
      ["independent session", policy.require_independent_session ?? policy.requireIndependentSession],
      ["clean environment", policy.require_clean_environment ?? policy.requireCleanEnvironment],
      ["distinct model profile", policy.require_distinct_model_profile ?? policy.requireDistinctModelProfile],
    ].filter(([, value]) => value !== undefined && value !== null);
    return `<div class="research-prerequisites"><h5>Proof prerequisites</h5><div>${rules.map(([name, value]) => `<span><b>${safe(name)}</b>${safe(value)}</span>`).join("") || "<span>No proof policy projected</span>"}</div>${requirements.length ? `<ul>${requirements.map(value => `<li>${safe(value)}</li>`).join("")}</ul>` : ""}</div>`;
  }

  function attemptDetail(attempt) {
    const environment = object(attempt.environment), references = refs(attempt.references), observations = list(attempt.observations).slice(0, 16), differences = list(attempt.environmentalDifferences).slice(0, 16);
    return `<li class="research-attempt ${safe(attempt.outcome || "unknown")}"><header><b>${safe(attempt.id || "attempt")}</b><span>${safe(attempt.outcome || "unknown")}</span></header><div><span>${safe(attempt.workerId || "worker unknown")}</span><span>${safe(attempt.sessionId || "session unknown")}</span><span>${safe(attempt.modelProfile || "profile unknown")}</span></div><dl><div><dt>environment</dt><dd>${safe(environment.id || "—")} · ${environment.clean === true ? "clean" : environment.clean === false ? "not clean" : "unknown"}</dd></div><div><dt>platform</dt><dd>${safe(environment.platform || "—")} / ${safe(environment.architecture || "—")}</dd></div><div><dt>isolation</dt><dd>${safe(environment.isolation || "—")}</dd></div></dl>${observations.length ? `<p>${observations.map(safe).join(" · ")}</p>` : ""}${differences.length ? `<p class="research-differences"><b>environment differences</b>${differences.map(safe).join(" · ")}</p>` : ""}${references.length ? `<footer>${refButtons(references)}</footer>` : ""}</li>`;
  }

  function dossierDetail(dossier, artifacts) {
    const verification = dossier.verified === true && dossier.verificationStatus === "verified" ? "hash verified" : dossier.verificationStatus === "stale-or-invalid" ? "stale or invalid" : "published · byte verification pending";
    const ids = Object.entries(object(dossier.artifactIds)).slice(0, 16).filter(([kind, id]) => stable(kind) && stable(id));
    const artifactIndex = new Map(artifacts.map(item => [item.id, item]));
    return `<article class="research-dossier ${safe(dossier.verificationStatus || "unknown")}"><header><button type="button" data-research-ref-kind="proof-bundle" data-research-ref-id="${safe(dossier.id)}">${safe(dossier.id)}</button><b>${safe(verification)}</b></header><div class="research-hashes"><code>manifest ${digest(dossier.manifestSha256) ? safe(dossier.manifestSha256) : "unverified"}</code><code>finding state ${digest(dossier.findingStateSha256) ? safe(dossier.findingStateSha256) : "unverified"}</code></div><div class="research-verdict"><span>${safe(dossier.verdict || "verdict unknown")}</span><span>${safe(dossier.findingStatus || "finding state unknown")}</span></div><ul>${ids.map(([kind, id]) => { const artifact = object(artifactIndex.get(id)); return `<li><button type="button" data-research-ref-kind="artifact" data-research-ref-id="${safe(id)}">${safe(kind)}</button><code>${digest(artifact.sha256) ? safe(artifact.sha256) : "hash unavailable"}</code><span>${Number.isInteger(artifact.size_bytes) ? artifact.size_bytes : Number.isInteger(artifact.sizeBytes) ? artifact.sizeBytes : "—"} bytes</span></li>`; }).join("") || "<li>No bounded dossier artifacts projected</li>"}</ul></article>`;
  }

  function evidenceDetail(record) {
    const original = digest(record.originalSha256) ? record.originalSha256 : null, display = digest(record.displaySha256) ? record.displaySha256 : null;
    return `<li class="research-evidence ${safe(record.state || "unknown")}"><button type="button" data-research-ref-kind="evidence" data-research-ref-id="${safe(record.artifactId)}">${safe(record.kind || "evidence")}</button><span>${record.redacted === true ? "redacted display projection" : "display projection"}${record.truncated === true ? " · truncated" : ""}</span><code>original ${safe(original || "hash unavailable")}</code><code>display ${safe(display || "hash unavailable")}</code>${list(record.quarantineLabels).length ? `<small>restricted · ${list(record.quarantineLabels).slice(0, 16).map(safe).join(" · ")}</small>` : ""}</li>`;
  }

  function proofDetail(item) {
    const attempts = item.attempts.map(attemptDetail).join(""), dossiers = item.dossiers.map(dossier => dossierDetail(dossier, item.proofArtifacts)).join(""), evidence = item.proofEvidence.map(evidenceDetail).join("");
    return `<details class="research-proof-detail"><summary>Unified proof detail <span>${item.dossiers.length} dossier · ${item.attempts.length} attempt · ${item.proofEvidence.length} evidence</span></summary>${proofPolicy(item.proofPolicy, item.recipe)}<section><h5>Reproduction & validation attempts</h5><ol>${attempts || "<li>No canonical attempts projected</li>"}</ol></section><section><h5>Proof manifests & dossiers</h5>${dossiers || "<p>No published proof dossier projected.</p>"}</section><section><h5>Artifact redaction & hashes</h5><ul>${evidence || "<li>No exact evidence projection is linked.</li>"}</ul></section></details>`;
  }

  function lanes(items, names, renderer, empty) {
    return names.map(name => { const values = items.filter(item => item.lane === name); return `<section class="research-lane ${safe(name)}" aria-label="${safe(name.replaceAll("-", " "))}"><header><span>${safe(name.replaceAll("-", " "))}</span><b>${values.length}</b></header><div>${values.length ? values.map(renderer).join("") : `<p class="research-empty">${safe(empty)}</p>`}</div></section>`; }).join("");
  }

  function render(snapshot) {
    const value = model(snapshot);
    return `<section class="research-workspace" aria-labelledby="researchWorkspaceHeading"><header class="research-head"><div><span class="eyebrow">EVIDENCE-DRIVEN WORKSPACE</span><h3 id="researchWorkspaceHeading">Research state</h3><p>Canonical hypotheses, discriminating tests, findings, and proof thresholds—not transcript claims.</p></div><div><span><b>${value.hypotheses.length}</b> hypotheses</span><span><b>${value.findings.length}</b> findings</span></div></header>${value.degraded ? `<div class="research-degraded"><b>Canonical outcome projection is degraded</b><span>Proof dossier joins are withheld rather than inferred.</span></div>` : ""}<div class="research-section"><h4>Hypothesis board</h4><div class="research-lanes hypotheses">${lanes(value.hypotheses, ["proposed","testing","supported","disproved","reproduction"], hypothesisCard, "No hypotheses")}</div></div><div class="research-section"><h4>Finding & proof board</h4><div class="research-lanes findings">${lanes(value.findings, ["candidate","demonstrated","reproduction-pending","reproduced","operator-accepted","rejected","withdrawn","inconclusive"], findingCard, "No findings")}</div></div></section>`;
  }
  return Object.freeze({authorityOperations, model, render});
});
