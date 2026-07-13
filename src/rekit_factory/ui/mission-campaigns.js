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
    return campaign.recommendation?.action === "ask-operator" && campaign.recommendationDisposition === "pending";
  }

  function renderCard(campaign, index = 0) {
    const id = campaign.campaignId || "unknown", status = String(campaign.status || "unknown"), health = object(campaign.health), visual = health.degraded ? "degraded" : tone(status), rows = budgetRows(campaign).slice(0, 4), scope = object(campaign.scope), epoch = object(campaign.currentEpoch), handoff = object(campaign.handoff);
    return `<article class="campaign-card ${safe(visual)}${needsAction(campaign) ? " needs-action" : ""}" style="--campaign-order:${index}" data-campaign="${safe(id)}" tabindex="0" role="button" aria-label="Open campaign ${safe(short(id))}">
      <header><div class="campaign-signal" aria-hidden="true"><i></i><span>◎</span></div><div class="campaign-identity"><span>CAMPAIGN · ${safe(short(id))}</span><b>${safe(`Epoch ${epoch.ordinal ?? "—"}`)}</b><small>${safe(epoch.epochId || "No active epoch")}</small></div><span class="campaign-state ${safe(visual)}">${safe(health.degraded ? `degraded · ${number(health.problemCount)}` : label(status))}</span></header>
      <p class="campaign-reason">${safe(reason(campaign))}</p>
      <div class="campaign-vitals"><span><b>${safe(epoch.ordinal ?? "—")}</b> epoch</span><span><b>${list(epoch.workIds).length}</b> work</span><span><b>${safe(campaign.revision ?? "—")}</b> revision</span><span><b>${safe(campaign.recommendationDisposition || "—")}</b> policy</span></div>
      <div class="campaign-budget-strip">${rows.length ? rows.map(row => `<span><i>${safe(row.name.replace(/([A-Z])/g, " $1"))}</i><b>${row.remaining}</b><small>${safe(row.unit || "left")}</small></span>`).join("") : `<span class="campaign-degraded">Budget projection unavailable</span>`}</div>
      <footer><span>scope r${safe(scope.revision ?? "—")} · ${safe(short(scope.digest))}</span><span>${number(handoff.evidenceCount) + number(handoff.factoryRunCount)} bounded links</span><b>${needsAction(campaign) ? "NEEDS YOU →" : `checkpoint ${safe(short(campaign.latestCheckpointId))}`}</b></footer>
    </article>`;
  }

  function renderDetail(campaign) {
    const id = campaign.campaignId || "unknown", status = String(campaign.status || "unknown"), health = object(campaign.health), visual = health.degraded ? "degraded" : tone(status), scope = object(campaign.scope), terminal = object(campaign.terminal), recommendation = object(campaign.recommendation), epoch = object(campaign.currentEpoch), handoff = object(campaign.handoff), rows = budgetRows(campaign), actions = canonicalActions(campaign);
    const evidence = list(handoff.evidenceIds).map(value => `<div class="campaign-reference"><span>Evidence</span><code>${safe(value)}</code></div>`);
    const runs = list(handoff.factoryRunIds).map(value => `<button class="campaign-link" type="button" data-campaign-link="activity" data-campaign-ref="${safe(value)}"><span>Run</span><code>${safe(value)}</code></button>`);
    return `<div class="campaign-detail-head"><div><span class="eyebrow">BOUNDED CAMPAIGN</span><h2 id="campaignDialogTitle">Campaign ${safe(short(id))}</h2><code>${safe(id)}</code></div><span class="campaign-state ${safe(visual)}">${safe(health.degraded ? `degraded · ${number(health.problemCount)}` : label(status))}</span></div>
      <div class="campaign-detail-ribbon"><div><span>canonical reason</span><b>${safe(reason(campaign))}</b></div><div><span>checkpoint</span><b>${safe(campaign.latestCheckpointId || "not published")}</b></div></div>
      <div class="campaign-detail-grid"><section><h3>Durable progress</h3><div class="campaign-metrics"><span><b>${safe(epoch.ordinal ?? "—")}</b>epoch</span><span><b>${list(epoch.workIds).length}</b>work items</span><span><b>${safe(campaign.revision ?? "—")}</b>revision</span><span><b>${safe(campaign.recommendationDisposition || "—")}</b>policy state</span><span><b>${number(handoff.evidenceCount)}</b>evidence</span><span><b>${number(handoff.factoryRunCount)}</b>runs</span></div></section><section><h3>Immutable scope</h3><div class="campaign-scope"><b>${safe(scope.scopeId || "No public scope ID")}</b><span>revision ${safe(scope.revision ?? "—")}</span><code>${safe(scope.digest || "digest unavailable")}</code></div></section></div>
      <section><h3>Remaining cumulative authority</h3><div class="campaign-budget-table">${rows.length ? rows.map(row => `<div><span>${safe(row.name.replace(/([A-Z])/g, " $1"))}</span><b>${row.remaining}</b><small>${row.used} used / ${row.limit} ${safe(row.unit)}</small></div>`).join("") : `<div class="empty compact"><b>Projection unavailable</b>No budget values were published.</div>`}</div></section>
      ${terminal.reasonCode ? `<section class="campaign-terminal"><h3>Terminal outcome</h3><div><b>${safe(terminal.status || campaign.status)}</b><span>${safe(terminal.reasonCode)}</span><code>${safe(terminal.finalCheckpointId || "no final checkpoint")}</code></div></section>` : ""}
      ${recommendation.recommendationId ? `<section class="campaign-recommendation"><h3>Exact policy recommendation</h3><div><b>${safe(recommendation.action || "pending")}</b><span>${safe(recommendation.reasonCode || "reason unavailable")}</span><code>${safe(recommendation.recommendationId)}</code></div></section>` : ""}
      <section><h3>Bounded handoff links</h3><div class="campaign-links">${[...evidence, ...runs].join("") || `<div class="empty compact"><b>No bounded links</b>Evidence and runs will appear here by stable ID.</div>`}</div></section>
      <div class="campaign-actions" data-campaign-actions>${actions.map(action => `<button class="btn ${action === "stop" ? "red" : action === "resume" ? "primary" : ""}" type="button" data-campaign-action="${safe(action)}" data-campaign-id="${safe(id)}">${safe(action[0].toUpperCase() + action.slice(1))}</button>`).join("") || `<span>No operator transition is currently allowed.</span>`}</div>`;
  }

  return {budgetRows, canonicalActions, needsAction, renderCard, renderDetail, statusTone: tone};
});
