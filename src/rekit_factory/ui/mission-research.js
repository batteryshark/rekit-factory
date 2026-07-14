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
  const refs = values => list(values).filter(value => value && typeof value.kind === "string" && typeof value.id === "string");
  const refButtons = values => refs(values).map(ref => `<button type="button" data-research-ref-kind="${safe(ref.kind)}" data-research-ref-id="${safe(ref.id)}">${safe(ref.kind)}:${safe(ref.id)}</button>`).join("");
  const hypothesisLane = status => status === "testing" ? "testing" : ["supported", "contradicted"].includes(status) ? "supported" : ["disproved", "retired"].includes(status) ? "disproved" : status === "reproduced" ? "reproduction" : "proposed";
  const findingLane = status => ["operator-accepted", "reproduced", "rejected", "withdrawn", "inconclusive"].includes(status) ? status : status || "candidate";

  function model(snapshot) {
    const hypothesisState = object(snapshot?.hypothesisState), findingState = object(snapshot?.findingState);
    const tests = list(hypothesisState.tests), observations = list(hypothesisState.observations), attempts = list(findingState.attempts);
    const hypotheses = list(hypothesisState.hypotheses).map(item => ({...item,
      lane: hypothesisLane(item.status), tests: tests.filter(test => test.hypothesisId === item.id),
      observations: observations.filter(observation => observation.hypothesisId === item.id),
    }));
    const findings = list(findingState.findings).map(item => ({...item,
      lane: findingLane(item.lifecycleStatus || item.status),
      attempts: attempts.filter(attempt => attempt.findingId === item.id),
    }));
    return {hypotheses, findings};
  }

  function hypothesisCard(item) {
    const stop = object(item.stopCondition), evidence = item.observations.flatMap(value => refs(value.references));
    const tests = item.tests.map(test => `<li><b>${safe(test.objective || test.id)}</b><span>${safe(test.status)} · priority ${safe(test.priority ?? "—")} · ${safe(test.attempts ?? 0)} attempts</span><p>Expect: ${safe(test.expected_observation || test.expectedObservation || "—")}</p><p>Falsify: ${safe(test.falsifying_observation || test.falsifyingObservation || "—")}</p></li>`).join("");
    return `<article class="research-card hypothesis ${safe(item.lane)}" data-research-hypothesis="${safe(item.id)}"><header><span>${safe(item.id)}</span><b>${safe(item.status)}</b></header><h4>${safe(item.claim)}</h4><div class="research-meter"><i style="--confidence:${Math.max(0, Math.min(100, Number(item.confidence || 0) * 100))}%"></i><span>${percent(item.confidence)} confidence</span></div><dl><div><dt>expect</dt><dd>${safe(item.expectedObservation || "—")}</dd></div><div><dt>falsifier</dt><dd>${safe(item.falsifier || "—")}</dd></div><div><dt>stop</dt><dd>${safe(stop.max_attempts ?? stop.maxAttempts ?? "—")} attempts · ${safe(stop.max_cost_units ?? stop.maxCostUnits ?? "—")} cost</dd></div><div><dt>workstream</dt><dd>${safe(item.ownerWorkstream || "unassigned")}</dd></div></dl>${item.tests.length ? `<ul class="research-tests">${tests}</ul>` : ""}<footer>${refButtons([...refs(item.references), ...evidence]) || "<span>No evidence references projected</span>"}</footer></article>`;
  }

  function findingCard(item) {
    const successes = item.attempts.filter(value => value.outcome === "success").length, policy = object(item.proofPolicy), decision = object(item.operatorDecision);
    return `<article class="research-card finding ${safe(item.lane)}" data-research-finding="${safe(item.id)}"><header><span>${safe(item.findingType || "finding")} · ${safe(item.consequence || "unknown")}</span><b>${safe(item.lifecycleStatus || item.status)}</b></header><h4>${safe(item.impactClaim || item.id)}</h4><p class="research-uncertainty"><b>Known uncertainty</b>${safe(item.knownUncertainty || "None projected")}</p><div class="research-proof"><span><b>${successes}/${safe(policy.successful_clean_reproductions ?? policy.successfulCleanReproductions ?? "—")}</b> clean reproductions</span><span><b>${item.attempts.length}</b> attempts</span><span><b>${percent(item.confidence)}</b> confidence</span></div>${decision.decision ? `<div class="research-decision"><b>operator ${safe(decision.decision)}</b><span>${safe(decision.rationale || "")}</span></div>` : ""}<footer>${refButtons(item.references) || "<span>No evidence references projected</span>"}<button type="button" data-research-outcome="${safe(item.id)}">Canonical outcome →</button></footer></article>`;
  }

  function lanes(items, names, renderer, empty) {
    return names.map(name => { const values = items.filter(item => item.lane === name); return `<section class="research-lane ${safe(name)}"><header><span>${safe(name.replaceAll("-", " "))}</span><b>${values.length}</b></header><div>${values.length ? values.map(renderer).join("") : `<p class="research-empty">${safe(empty)}</p>`}</div></section>`; }).join("");
  }

  function render(snapshot) {
    const value = model(snapshot);
    return `<section class="research-workspace" aria-labelledby="researchWorkspaceHeading"><header class="research-head"><div><span class="eyebrow">EVIDENCE-DRIVEN WORKSPACE</span><h3 id="researchWorkspaceHeading">Research state</h3><p>Canonical hypotheses, discriminating tests, findings, and proof thresholds—not transcript claims.</p></div><div><span><b>${value.hypotheses.length}</b> hypotheses</span><span><b>${value.findings.length}</b> findings</span></div></header><div class="research-section"><h4>Hypothesis board</h4><div class="research-lanes hypotheses">${lanes(value.hypotheses, ["proposed","testing","supported","disproved","reproduction"], hypothesisCard, "No hypotheses")}</div></div><div class="research-section"><h4>Finding & proof board</h4><div class="research-lanes findings">${lanes(value.findings, ["candidate","demonstrated","reproduction-pending","reproduced","operator-accepted","rejected","withdrawn","inconclusive"], findingCard, "No findings")}</div></div></section>`;
  }
  return Object.freeze({model, render});
});
