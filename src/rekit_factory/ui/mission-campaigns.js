"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.MissionCampaigns = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const safe = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[character]);
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const list = value => Array.isArray(value) ? value : [];
  const label = value => String(value || "unknown").replaceAll("-", " ");
  const tone = value => ({running: "productive", completed: "successful", waiting: "waiting", suspended: "waiting", exhausted: "exhausted", blocked: "blocked", "policy-stopped": "policy-stopped", failed: "failed", stopped: "blocked", requested: "waiting"})[value] || "degraded";
  const limit = value => number(object(value).value ?? value);
  const short = value => String(value || "").slice(0, 12) || "—";
  const stableId = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
  const reason = campaign => campaign.terminal?.reasonCode || campaign.recommendation?.reasonCode || campaign.handoff?.reasonCode || "No canonical reason published";

  function budgetRows(campaign) {
    const budget = object(campaign.budget), remaining = object(budget.remaining), ceiling = object(budget.cumulative), usage = object(campaign.cumulativeUsage);
    const names = ["workItems", "costUnits", "wallSeconds", "toolCalls", "inputTokens", "outputTokens", "retries", "artifactBytes", "networkCalls"];
    return names.filter(name => name in remaining || name in ceiling || name in usage).map(name => ({name, remaining: number(remaining[name]), limit: limit(ceiling[name]), used: number(usage[name]), unit: object(ceiling[name]).unit || ""}));
  }

  function canonicalActions(campaign) {
    if (campaign.health?.degraded === true) return [];
    return list(campaign.allowedActions).filter(action => ["pause", "resume", "stop"].includes(action));
  }

  function needsAction(campaign) {
    return (campaign.recommendation?.action === "ask-operator" && campaign.recommendationDisposition === "pending") || pendingChanges(campaign).length > 0;
  }

  function pendingChanges(campaign) {
    return list(campaign.changeRequests).filter(request => {
      const value = object(request);
      return value.status === "pending" && stableId(value.requestId)
        && stableId(value.currentCampaignId) && stableId(value.proposedCampaignId)
        && Number.isInteger(value.revision) && value.revision >= 0
        && value.currentCampaignId === campaign.campaignId;
    }).slice(0, 32);
  }

  function changeDecisionPayload(campaign, requestId, approved) {
    if (typeof approved !== "boolean") return null;
    const request = pendingChanges(campaign).find(item => item.requestId === requestId);
    if (!request) return null;
    const expectedRevision = request.revision;
    const operationId = `mission-control:${campaign.campaignId}:change:${approved ? "approve" : "reject"}:${expectedRevision}:${requestId}`;
    return {requestId, approved, expectedRevision, operationId};
  }

  const CHANGE_BUDGET_FIELDS = ["workItems", "concurrency", "retries", "inputTokens", "outputTokens", "costUnits", "wallSeconds", "toolCalls", "networkCalls", "artifactBytes"];
  const scalar = value => Array.isArray(value)
    ? (value.length <= 256 && value.every(stableId) ? value.join(", ") || "none" : "—")
    : ["string", "number", "boolean"].includes(typeof value) ? String(value) : "—";
  const pair = (name, values) => {
    const item = object(values);
    const current = scalar(item.current), proposed = scalar(item.proposed);
    return `<div class="campaign-change-row ${current === proposed ? "unchanged" : "changed"}"><span>${safe(name)}</span><code>${safe(current)}</code><i aria-hidden="true">→</i><code>${safe(proposed)}</code></div>`;
  };
  function budgetDiff(name, values) {
    const section = object(values), current = object(section.current), proposed = object(section.proposed);
    const rows = CHANGE_BUDGET_FIELDS.flatMap(field => {
      const before = object(current[field]), after = object(proposed[field]);
      if (!(field in current) && !(field in proposed)) return [];
      return [pair(field.replace(/([A-Z])/g, " $1"), {current: `${scalar(before.value)} ${scalar(before.unit)}`, proposed: `${scalar(after.value)} ${scalar(after.unit)}`})];
    });
    return rows.length ? `<section class="campaign-change-section"><h4>${safe(name)}</h4>${rows.join("")}</section>` : "";
  }
  function objectDiff(name, values, fields) {
    const section = object(values), current = object(section.current), proposed = object(section.proposed);
    const rows = fields.flatMap(([field, title]) => (field in current || field in proposed) ? [pair(title, {current: current[field], proposed: proposed[field]})] : []);
    return rows.length ? `<section class="campaign-change-section"><h4>${safe(name)}</h4>${rows.join("")}</section>` : "";
  }
  function renderChangeRequest(request, campaignId) {
    const diff = object(request.diff), scope = object(diff.scope);
    const components = object(diff.componentVersions);
    const componentText = value => list(value).slice(0, 64).map(item => {
      const record = object(item);
      return stableId(record.name) && stableId(record.version) && /^[0-9a-f]{64}$/.test(record.digest) ? `${record.name}@${record.version} · ${record.digest}` : null;
    }).filter(Boolean).join(", ") || "—";
    const scopeHTML = pair("campaign identity", {current: request.currentCampaignId, proposed: request.proposedCampaignId}) + objectDiff("Scope binding", scope, [["scopeId", "scope ID"], ["revision", "revision"], ["digest", "content digest"]]);
    const completionHTML = objectDiff("Completion criteria", diff.completion, [["coverageBasisPoints", "coverage basis points"], ["resolvedHypotheses", "resolved hypotheses"], ["reproducedFindings", "reproduced findings"], ["requiredArtifactIds", "required artifacts"]]);
    const policyHTML = objectDiff("Operator policy", diff.operatorPolicy, [["scopeExpansionRequiresApproval", "scope approval"], ["hardCeilingIncreaseRequiresApproval", "ceiling approval"], ["continueAfterRiskRequiresApproval", "risk approval"], ["riskThreshold", "risk threshold"]]);
    const componentHTML = pair("bound components", {current: componentText(components.current), proposed: componentText(components.proposed)});
    return `<article class="campaign-change" data-campaign-change="${safe(request.requestId)}"><header><div><span>EXACT AUTHORITY REQUEST</span><b>Review campaign change</b></div><code>${safe(request.requestId)}</code></header><div class="campaign-change-legend"><span>current</span><i></i><span>proposed</span></div><div class="campaign-change-sections">${scopeHTML}${budgetDiff("Epoch ceilings", diff.epochBudget)}${budgetDiff("Cumulative ceilings", diff.cumulativeBudget)}${completionHTML}${policyHTML}<section class="campaign-change-section"><h4>Component versions</h4>${componentHTML}</section></div><div class="campaign-change-actions"><button class="btn red" type="button" data-campaign-change-decision="reject" data-campaign-id="${safe(campaignId)}" data-change-request="${safe(request.requestId)}" data-change-revision="${safe(request.revision)}">Reject</button><button class="btn primary" type="button" data-campaign-change-decision="approve" data-campaign-id="${safe(campaignId)}" data-change-request="${safe(request.requestId)}" data-change-revision="${safe(request.revision)}">Approve exact request</button></div></article>`;
  }

  function typedLinks(campaign) {
    const allowed = {evidence: "artifacts", hypothesis: "outcomes", finding: "outcomes", "operator-decision": "outcomes", "proof-bundle": "dossiers"};
    return list(object(campaign.typedLinks).references).flatMap(value => {
      const reference = object(value), kind = String(reference.kind || ""), surface = allowed[kind];
      if (!surface || reference.surface !== surface || typeof reference.entityId !== "string" || !reference.entityId || typeof reference.runId !== "string" || !reference.runId) return [];
      return [{kind, surface, entityId: reference.entityId, runId: reference.runId}];
    });
  }

  function strongestResult(campaign) {
    const value = object(object(campaign.typedLinks).strongestReproducedResult);
    const allowedBasis = new Set(["reproduced", "operator-accepted", "reproduced-published-proof", "operator-accepted-published-proof"]);
    const expectedSurface = value.kind === "proof-bundle" ? "dossiers" : value.kind === "finding" ? "outcomes" : null;
    if (!expectedSurface || value.surface !== expectedSurface || !allowedBasis.has(value.basis)
        || !stableId(value.entityId) || !stableId(value.findingId) || !stableId(value.runId)) return null;
    const exactLink = typedLinks(campaign).some(link => link.kind === value.kind
      && link.surface === value.surface && link.entityId === value.entityId
      && link.runId === value.runId);
    return exactLink ? {kind: value.kind, surface: value.surface, entityId: value.entityId,
      findingId: value.findingId, runId: value.runId, basis: value.basis} : null;
  }

  function currentResearchFocus(campaign) {
    const value = object(object(campaign.typedLinks).currentResearchFocus);
    if (value.surface !== "outcomes" || value.phase !== "testing"
        || !stableId(value.runId) || !stableId(value.hypothesisId) || !stableId(value.testId)
        || typeof value.hypothesisClaim !== "string" || !value.hypothesisClaim.trim()
        || value.hypothesisClaim.length > 400 || typeof value.objective !== "string"
        || !value.objective.trim() || value.objective.length > 280
        || typeof value.textTruncated !== "boolean" || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value.hypothesisClaim + value.objective)) return null;
    const linked = typedLinks(campaign).some(link => link.kind === "hypothesis"
      && link.surface === "outcomes" && link.entityId === value.hypothesisId
      && link.runId === value.runId);
    return linked ? {runId: value.runId, hypothesisId: value.hypothesisId,
      testId: value.testId, surface: "outcomes", phase: "testing",
      hypothesisClaim: value.hypothesisClaim, objective: value.objective,
      textTruncated: value.textTruncated} : null;
  }

  function synthesis(campaigns) {
    return list(campaigns).flatMap(campaign => {
      if (!stableId(campaign?.campaignId)) return [];
      const degraded = campaign.health?.degraded === true;
      const facts = degraded ? [] : healthFacts(campaign);
      const budgets = degraded ? [] : budgetRows(campaign).filter(row => ["workItems", "costUnits", "wallSeconds"].includes(row.name));
      return [{campaignId: campaign.campaignId, status: String(campaign.status || "unknown"),
        degraded, needsAction: !degraded && needsAction(campaign), reason: reason(campaign),
        progress: Object.fromEntries(facts.map(item => [item.name, item.value])), budgets,
        strongest: degraded ? null : strongestResult(campaign),
        focus: degraded ? null : currentResearchFocus(campaign)}];
    }).sort((left, right) => Number(right.needsAction) - Number(left.needsAction)
      || Number(right.degraded) - Number(left.degraded)
      || left.campaignId.localeCompare(right.campaignId));
  }

  function renderSynthesis(campaigns) {
    const rows = synthesis(campaigns);
    if (!rows.length) return `<div class="campaign-synthesis-empty"><b>No campaign synthesis yet</b><span>Canonical campaign projections will appear here.</span></div>`;
    return `<div class="campaign-synthesis-head"><span>operator scan</span><b>${rows.filter(row => row.needsAction).length} need action</b></div><div class="campaign-synthesis-list">${rows.map(row => {
      const focus = row.focus ? `<button type="button" class="campaign-synthesis-focus" data-campaign-link="outcomes" data-campaign-kind="hypothesis" data-campaign-ref="${safe(row.focus.hypothesisId)}" data-campaign-run="${safe(row.focus.runId)}"><span>testing now · ${safe(row.focus.hypothesisId)}</span><b>${safe(row.focus.objective)}</b><small>${safe(row.focus.hypothesisClaim)}${row.focus.textTruncated ? "…" : ""} · ${safe(row.focus.testId)} →</small></button>` : `<div class="campaign-synthesis-focus empty"><span>testing now</span><b>not singular</b><small>no exact current focus</small></div>`;
      const progress = row.degraded ? `<span class="degraded">canonical health unavailable</span>` : `<span><b>${safe(row.progress.coverage || "—")}</b> coverage</span><span><b>${safe(row.progress["novel / total"] || "—")}</b> novel / total</span>`;
      const budgets = row.budgets.length ? row.budgets.map(item => `<span><b>${safe(item.remaining)}</b> ${safe(item.name.replace(/([A-Z])/g, " $1"))} left</span>`).join("") : `<span><b>—</b> bounded budget unavailable</span>`;
      const result = row.strongest ? `<button type="button" class="campaign-synthesis-result" data-campaign-link="${safe(row.strongest.surface)}" data-campaign-kind="${safe(row.strongest.kind)}" data-campaign-ref="${safe(row.strongest.entityId)}" data-campaign-run="${safe(row.strongest.runId)}"><span>strongest reproduced</span><b>${safe(row.strongest.findingId)}</b><small>${safe(label(row.strongest.basis))} →</small></button>` : `<div class="campaign-synthesis-result empty"><span>strongest reproduced</span><b>none qualified</b><small>waiting for canonical proof</small></div>`;
      return `<article class="campaign-synthesis-row${row.needsAction ? " needs-action" : ""}${row.degraded ? " degraded" : ""}"><button type="button" class="campaign-synthesis-identity" data-campaign="${safe(row.campaignId)}"><span>${safe(short(row.campaignId))}</span><b>${row.needsAction ? "NEEDS YOU" : safe(label(row.status))}</b><small>${safe(row.reason)}</small></button>${focus}<div class="campaign-synthesis-progress">${progress}</div><div class="campaign-synthesis-budget">${budgets}</div>${result}</article>`;
    }).join("")}</div>`;
  }

  function healthFacts(campaign) {
    const health = object(campaign.health), current = object(health.current);
    if (health.degraded === true) return [];
    if (!Number.isInteger(current.sequence) || current.sequence < 1) return [];
    const fact = (name, value) => ({name, value});
    const integer = value => Number.isInteger(value) && value >= 0 ? String(value) : "—";
    const coverage = Number.isInteger(current.coverageBasisPoints) && current.coverageBasisPoints >= 0 && current.coverageBasisPoints <= 10000
      ? `${(current.coverageBasisPoints / 100).toFixed(2).replace(/\.00$/, "")}%` : "—";
    const elapsed = integer(current.elapsedWallSeconds);
    const checkpoint = current.nextCheckpointExpectedWallSeconds === null ? "not scheduled"
      : Number.isInteger(current.nextCheckpointExpectedWallSeconds) && current.nextCheckpointExpectedWallSeconds >= 0 ? `${current.nextCheckpointExpectedWallSeconds}s cumulative` : "—";
    return [
      fact("phase", ["recon", "hypothesis", "validation"].includes(current.phase) ? current.phase : "—"), fact("coverage", coverage),
      fact("novel / total", `${integer(current.epochNovelProgress)} / ${integer(current.cumulativeNovelProgress)}`),
      fact("no progress / retry", `${integer(current.noProgressCount)} / ${integer(current.retryCount)}`),
      fact("elapsed", elapsed === "—" ? elapsed : `${elapsed}s`),
      fact("next checkpoint", checkpoint),
    ];
  }

  function renderCard(campaign, index = 0) {
    const id = campaign.campaignId || "unknown", status = String(campaign.status || "unknown"), health = object(campaign.health), visual = health.degraded ? "degraded" : tone(status), rows = budgetRows(campaign).slice(0, 4), scope = object(campaign.scope), epoch = object(campaign.currentEpoch), handoff = object(campaign.handoff), facts = healthFacts(campaign);
    return `<article class="campaign-card ${safe(visual)}${needsAction(campaign) ? " needs-action" : ""}" style="--campaign-order:${index}" data-campaign="${safe(id)}" tabindex="0" role="button" aria-label="Open campaign ${safe(short(id))}">
      <header><div class="campaign-signal" aria-hidden="true"><i></i><span>◎</span></div><div class="campaign-identity"><span>CAMPAIGN · ${safe(short(id))}</span><b>${safe(`Epoch ${epoch.ordinal ?? "—"}`)}</b><small>${safe(epoch.epochId || "No active epoch")}</small></div><span class="campaign-state ${safe(visual)}">${safe(health.degraded ? `degraded · ${number(health.problemCount)}` : label(status))}</span></header>
      <p class="campaign-reason">${safe(reason(campaign))}</p>
      <div class="campaign-vitals"><span><b>${safe(epoch.ordinal ?? "—")}</b> epoch</span><span><b>${list(epoch.workIds).length}</b> work</span><span><b>${safe(campaign.revision ?? "—")}</b> revision</span><span><b>${safe(campaign.recommendationDisposition || "—")}</b> policy</span></div>
      ${facts.length ? `<div class="campaign-health-strip">${facts.slice(0, 4).map(item => `<span><i>${safe(item.name)}</i><b>${safe(item.value)}</b></span>`).join("")}</div>` : ""}
      <div class="campaign-budget-strip">${rows.length ? rows.map(row => `<span><i>${safe(row.name.replace(/([A-Z])/g, " $1"))}</i><b>${row.remaining}</b><small>${safe(row.unit || "left")}</small></span>`).join("") : `<span class="campaign-degraded">Budget projection unavailable</span>`}</div>
      <footer><span>scope r${safe(scope.revision ?? "—")} · ${safe(short(scope.digest))}</span><span>${number(handoff.evidenceCount) + number(handoff.factoryRunCount)} bounded links</span><b>${needsAction(campaign) ? "NEEDS YOU →" : `checkpoint ${safe(short(campaign.latestCheckpointId))}`}</b></footer>
    </article>`;
  }

  function renderDetail(campaign) {
    const id = campaign.campaignId || "unknown", status = String(campaign.status || "unknown"), health = object(campaign.health), visual = health.degraded ? "degraded" : tone(status), scope = object(campaign.scope), terminal = object(campaign.terminal), recommendation = object(campaign.recommendation), epoch = object(campaign.currentEpoch), handoff = object(campaign.handoff), rows = budgetRows(campaign), actions = canonicalActions(campaign), facts = healthFacts(campaign), changes = pendingChanges(campaign);
    const evidence = list(handoff.evidenceIds).map(value => `<div class="campaign-reference"><span>Evidence</span><code>${safe(value)}</code></div>`);
    const runs = list(handoff.factoryRunIds).map(value => `<button class="campaign-link" type="button" data-campaign-link="activity" data-campaign-ref="${safe(value)}"><span>Run</span><code>${safe(value)}</code></button>`);
    const typed = typedLinks(campaign).map(reference => `<div class="campaign-typed-link"><button class="campaign-link" type="button" data-campaign-link="${safe(reference.surface)}" data-campaign-kind="${safe(reference.kind)}" data-campaign-ref="${safe(reference.entityId)}" data-campaign-run="${safe(reference.runId)}"><span>${safe(label(reference.kind))}</span><code>${safe(reference.entityId)}</code></button><button class="campaign-copy" type="button" data-campaign-copy="${safe(`${reference.kind}:${reference.entityId}`)}" aria-label="Copy ${safe(label(reference.kind))} identifier">Copy ID</button></div>`);
    return `<div class="campaign-detail-head"><div><span class="eyebrow">BOUNDED CAMPAIGN</span><h2 id="campaignDialogTitle">Campaign ${safe(short(id))}</h2><code>${safe(id)}</code></div><span class="campaign-state ${safe(visual)}">${safe(health.degraded ? `degraded · ${number(health.problemCount)}` : label(status))}</span></div>
      <div class="campaign-detail-ribbon"><div><span>canonical reason</span><b>${safe(reason(campaign))}</b></div><div><span>checkpoint</span><b>${safe(campaign.latestCheckpointId || "not published")}</b></div></div>
      <div class="campaign-detail-grid"><section><h3>Durable progress</h3><div class="campaign-metrics"><span><b>${safe(epoch.ordinal ?? "—")}</b>epoch</span><span><b>${list(epoch.workIds).length}</b>work items</span><span><b>${safe(campaign.revision ?? "—")}</b>revision</span><span><b>${safe(campaign.recommendationDisposition || "—")}</b>policy state</span><span><b>${number(handoff.evidenceCount)}</b>evidence</span><span><b>${number(handoff.factoryRunCount)}</b>runs</span></div></section><section><h3>Immutable scope</h3><div class="campaign-scope"><b>${safe(scope.scopeId || "No public scope ID")}</b><span>revision ${safe(scope.revision ?? "—")}</span><code>${safe(scope.digest || "digest unavailable")}</code></div></section></div>
      ${facts.length ? `<section><h3>Canonical health observation</h3><div class="campaign-health-detail">${facts.map(item => `<span><i>${safe(item.name)}</i><b>${safe(item.value)}</b></span>`).join("")}</div></section>` : ""}
      <section><h3>Remaining cumulative authority</h3><div class="campaign-budget-table">${rows.length ? rows.map(row => `<div><span>${safe(row.name.replace(/([A-Z])/g, " $1"))}</span><b>${row.remaining}</b><small>${row.used} used / ${row.limit} ${safe(row.unit)}</small></div>`).join("") : `<div class="empty compact"><b>Projection unavailable</b>No budget values were published.</div>`}</div></section>
      ${terminal.reasonCode ? `<section class="campaign-terminal"><h3>Terminal outcome</h3><div><b>${safe(terminal.status || campaign.status)}</b><span>${safe(terminal.reasonCode)}</span><code>${safe(terminal.finalCheckpointId || "no final checkpoint")}</code></div></section>` : ""}
      ${recommendation.recommendationId ? `<section class="campaign-recommendation"><h3>Exact policy recommendation</h3><div><b>${safe(recommendation.action || "pending")}</b><span>${safe(recommendation.reasonCode || "reason unavailable")}</span><code>${safe(recommendation.recommendationId)}</code></div></section>` : ""}
      ${changes.length ? `<section class="campaign-change-stage"><h3>Pending authority changes</h3>${changes.map(request => renderChangeRequest(request, id)).join("")}</section>` : ""}
      <section><h3>Canonical record links</h3><div class="campaign-links">${typed.join("") || `<div class="empty compact"><b>No resolved records</b>Typed links appear only when a campaign-owned run proves the canonical association.</div>`}</div></section>
      <section><h3>Bounded handoff identifiers</h3><div class="campaign-links">${[...evidence, ...runs].join("") || `<div class="empty compact"><b>No bounded links</b>Evidence and runs will appear here by stable ID.</div>`}</div></section>
      <div class="campaign-actions" data-campaign-actions>${actions.map(action => `<button class="btn ${action === "stop" ? "red" : action === "resume" ? "primary" : ""}" type="button" data-campaign-action="${safe(action)}" data-campaign-id="${safe(id)}">${safe(action[0].toUpperCase() + action.slice(1))}</button>`).join("") || `<span>No operator transition is currently allowed.</span>`}</div>`;
  }

  return {budgetRows, canonicalActions, changeDecisionPayload, currentResearchFocus, healthFacts, needsAction, pendingChanges, renderCard, renderChangeRequest, renderDetail, renderSynthesis, statusTone: tone, strongestResult, synthesis, typedLinks};
});
