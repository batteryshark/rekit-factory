"use strict";

const MissionObservability = (() => {
  const safe = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[character]);
  const first = (object, ...keys) => keys.map(key => object?.[key]).find(value => value !== undefined && value !== null);
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const eventTone = (kind = "") => { const value = kind.toLowerCase(); if (value.includes("thinking") || value.includes("reason")) return "thinking"; if (value.includes("tool")) return "tool"; if (value.includes("retry") || value.includes("failed") || value.includes("error")) return "retry"; if (value.includes("valid") || value.includes("schema")) return "validation"; if (value.includes("model") || value.includes("worker")) return "model"; return "system"; };
  const payloadFacts = payload => {
    if (!payload || typeof payload !== "object") return [];
    const labels = {attempt: "attempt", characters: "chars", toolId: "tool", tool_id: "tool", toolName: "call", tool_name: "call", toolCount: "tools", observationCount: "observations", nextActionCount: "next", inputTokens: "input", outputTokens: "output", cacheReadTokens: "cache read", cacheWriteTokens: "cache write"};
    return Object.entries(labels).flatMap(([key, label]) => payload[key] === undefined || payload[key] === null || typeof payload[key] === "object" ? [] : [[label, payload[key]]]).slice(0, 4);
  };
  function renderEvent(event) {
    const kind = String(first(event, "kind", "type") || "event"), tone = eventTone(kind), created = first(event, "created_at", "createdAt", "timestamp"), worker = first(event, "worker_id", "workerId");
    const facts = payloadFacts(event.payload).map(([label, value]) => `<span><b>${safe(label)}</b> ${safe(value)}</span>`).join("");
    return `<article class="event event-${tone}"><div class="event-rail"><i></i></div><time>${safe(created ? new Date(created).toLocaleTimeString() : "—")}</time><div class="event-content"><div class="event-heading"><span class="event-kind">${safe(kind.replaceAll(".", " / "))}</span>${worker ? `<span class="event-worker">${safe(worker)}</span>` : ""}</div><div class="event-message">${safe(first(event, "message", "summary") || kind)}</div>${facts ? `<div class="event-facts">${facts}</div>` : ""}</div></article>`;
  }
  function activitySummary(events = []) {
    const counts = {model: 0, thinking: 0, tool: 0, validation: 0, retry: 0};
    events.forEach(event => { const tone = eventTone(first(event, "kind", "type") || ""); if (tone in counts) counts[tone] += 1; });
    return Object.entries(counts).map(([kind, count]) => `<div class="activity-stat ${kind}"><b>${count}</b><span>${safe(kind)}</span></div>`).join("");
  }
  function reports(snapshot) {
    if (Array.isArray(snapshot.workerReports)) return snapshot.workerReports.map(report => ({
      role: report.role, title: report.title, summary: report.summary,
      observations: report.observations || [], next: report.nextActions || [],
      workerNote: report.workerNote, identity: report.identity,
      facets: MissionOutcomes.reportFacets(report),
    }));
    return [];
  }
  function renderReports(snapshot) {
    const items = reports(snapshot);
    if (!items.length) return `<div class="empty compact"><b>No worker reports yet</b>Completed structured worker results will collect here.</div>`;
    return items.map(report => `<article class="report-card"><header><div><span class="report-role">${safe(report.role)}</span><h3>${safe(report.title)}</h3><small>${safe(report.identity?.entityType)} · ${safe(report.identity?.entityId)}</small></div></header><div class="report-section"><b>Canonical facets</b><div class="report-facets">${report.facets.map(facet => `<span class="report-status">${safe(facet.name)}: ${safe(facet.state)}</span>`).join("")}</div></div><p>${safe(report.summary)}</p>${report.workerNote ? `<div class="report-section"><b>Worker note (unverified)</b><p>${safe(report.workerNote)}</p></div>` : ""}${report.observations.length ? `<div class="report-section"><b>Observations</b><ul>${report.observations.map(item => `<li>${safe(item)}</li>`).join("")}</ul></div>` : ""}${report.next.length ? `<div class="report-section next"><b>Next actions</b><ul>${report.next.map(item => `<li>${safe(item)}</li>`).join("")}</ul></div>` : ""}</article>`).join("");
  }
  const tokenValue = (usage, names) => { for (const name of names) if (usage?.[name] !== undefined) return number(usage[name]); return 0; };
  function usageRows(snapshot) {
    return (snapshot.modelCalls || []).map(call => { const usage = call.usage || {}; return {provider: call.provider || "provider", model: call.model || snapshot.meta?.modelProfile?.model || "model", purpose: call.purpose || "worker", input: tokenValue(usage, ["inputTokens", "input_tokens", "request_tokens"]), output: tokenValue(usage, ["outputTokens", "output_tokens", "response_tokens"]), cacheRead: tokenValue(usage, ["cacheReadTokens", "cache_read_tokens", "cacheReadInputTokens", "cache_read_input_tokens"]), cacheWrite: tokenValue(usage, ["cacheWriteTokens", "cache_write_tokens", "cacheCreationInputTokens", "cache_creation_input_tokens"])}; });
  }
  function renderUsage(snapshot) {
    const rows = usageRows(snapshot), totals = rows.reduce((sum, row) => ({input: sum.input + row.input, output: sum.output + row.output, cacheRead: sum.cacheRead + row.cacheRead, cacheWrite: sum.cacheWrite + row.cacheWrite}), {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}), format = value => new Intl.NumberFormat().format(value);
    const summary = `<div class="usage-summary"><div><b>${rows.length}</b><span>model calls</span></div><div><b>${format(totals.input)}</b><span>input tokens</span></div><div><b>${format(totals.output)}</b><span>output tokens</span></div><div class="cache"><b>${format(totals.cacheRead)}</b><span>cache read</span></div><div class="cache"><b>${format(totals.cacheWrite)}</b><span>cache write</span></div></div>`;
    if (!rows.length) return `${summary}<div class="empty compact"><b>No model usage yet</b>Provider-neutral call accounting will appear after the first worker turn.</div>`;
    return `${summary}<div class="usage-table" role="table" aria-label="Model usage by call"><div class="usage-row usage-head" role="row"><span>purpose</span><span>model</span><span>input</span><span>output</span><span>cache read</span><span>cache write</span></div>${rows.map(row => `<div class="usage-row" role="row"><span><b>${safe(row.purpose)}</b><small>${safe(row.provider)}</small></span><span>${safe(row.model)}</span><span>${format(row.input)}</span><span>${format(row.output)}</span><span class="cache-value">${format(row.cacheRead)}</span><span class="cache-value">${format(row.cacheWrite)}</span></div>`).join("")}</div>`;
  }
  function questionVariant(question) {
    const kind = String(first(question, "kind", "type") || "decision").toLowerCase();
    if (kind.includes("permission") || first(question, "toolId", "tool_id", "safetyTier", "safety_tier")) return "permission";
    if (kind.includes("missing") || kind.includes("tool")) return "missing-tool";
    if (kind.includes("direction") || kind.includes("choice") || kind.includes("clarif")) return "direction";
    return "decision";
  }
  const optionParts = option => typeof option === "object" && option ? {value: first(option, "value", "id", "answer", "label"), label: first(option, "label", "title", "value", "id")} : {value: option, label: option};
  function renderDecision(runId, question) {
    const variant = questionVariant(question), kind = first(question, "kind", "type") || variant, prompt = first(question, "prompt", "question", "message", "title") || "Operator input required";
    const rawOptions = Array.isArray(question.options) ? question.options : Array.isArray(question.choices) ? question.choices : [], options = rawOptions.map(optionParts).filter(option => option.value !== undefined && option.value !== null);
    const context = [["tool", first(question, "toolId", "tool_id")], ["safety", first(question, "safetyTier", "safety_tier")], ["reason", first(question, "reason", "description")]].filter(([, value]) => value !== undefined && value !== null);
    const choices = options.length ? options.map((option, index) => `<button class="btn ${String(option.value).toLowerCase() === "allow" ? "primary" : index === 0 && variant === "direction" ? "primary" : String(option.value).toLowerCase() === "deny" ? "red" : ""}" data-answer="${safe(option.value)}" data-run="${safe(runId)}" data-question="${safe(question.id)}">${safe(option.label)}</button>`).join("") : variant === "direction" || variant === "decision" ? `<div class="direction-response"><label><span>Your direction</span><textarea data-direction-input maxlength="8000" placeholder="Give the investigation concrete direction…"></textarea></label><button class="btn primary" data-direction-submit data-run="${safe(runId)}" data-question="${safe(question.id)}">Send direction</button></div>` : `<span class="decision-unavailable">No response options supplied by this question.</span>`;
    return `<article class="decision decision-${variant}" data-decision-id="${safe(question.id)}" tabindex="-1"><div class="decision-head"><div class="decision-icon" aria-hidden="true">${variant === "permission" ? "!" : variant === "missing-tool" ? "?" : variant === "direction" ? "↗" : "◇"}</div><div><b>${safe(String(kind).replaceAll("_", " "))}</b><span>${safe(runId)}</span></div></div><div class="decision-body"><div class="question">${safe(prompt)}</div>${context.length ? `<div class="decision-context">${context.map(([label, value]) => `<span><b>${safe(label)}</b>${safe(value)}</span>`).join("")}</div>` : ""}<div class="choices">${choices}</div></div></article>`;
  }
  return {activitySummary, renderDecision, renderEvent, renderReports, renderUsage, reportCount: snapshot => reports(snapshot).length};
})();

const state = {fleet: [], campaigns: [], campaignSelected: null, campaignListRequest: 0, campaignDetailRequest: 0, campaignAction: null, campaignReturnFocus: null, config: null, filter: "all", query: "", selected: null, snapshot: null, evidence: [], notifications: [], notificationRun: null, notificationConfiguration: null, stream: null, streamCursors: new Map(), restarting: false, attention: MissionAttention.createTracker(), attentionReturnFocus: null, viewGeneration: 0, runRequests: MissionOutcomes.createGenerationGate(), snapshotRefreshes: MissionOutcomes.createGenerationGate(), outcomes: {tracker: MissionOutcomes.createSemanticTracker(), projection: null, integrity: "missing", renders: MissionOutcomes.createGenerationGate(), filters: {query: "", type: "all", state: "all", owner: "all", terminal: "all"}}};
const $ = id => document.getElementById(id);
const numeric = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
})[character]);
const THEME_KEY = "rekit-factory-theme";
const REFRESH_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>`;

function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch (_error) { return null; }
}

function applyTheme(theme, {persist = false} = {}) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = resolved;
  const toggle = $("themeToggle"), next = resolved === "dark" ? "light" : "dark";
  if (toggle) {
    toggle.setAttribute("aria-label", `Switch to ${next} theme`);
    toggle.title = `Switch to ${next} theme`;
  }
  if (persist) {
    try { localStorage.setItem(THEME_KEY, resolved); } catch (_error) { /* Theme persistence is optional. */ }
  }
  return resolved;
}

function initializeTheme() {
  const stored = storedTheme();
  const system = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  return applyTheme(stored || system);
}

function toggleTheme() {
  return applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light", {persist: true});
}

async function api(path, options) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function toast(message, error = false) {
  const element = $("toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 3200);
}

function dismissAttention({restoreFocus = true} = {}) {
  const element = $("operatorAttention");
  cancelAnimationFrame(showAttention.frame);
  clearTimeout(showAttention.announcementTimer);
  $("operatorAttentionAnnouncer").textContent = "";
  element.classList.remove("show");
  element.hidden = true;
  const previous = state.attentionReturnFocus;
  state.attentionReturnFocus = null;
  if (restoreFocus) {
    const activeView = document.querySelector(".view.active");
    const heading = activeView?.querySelector("h1, h2");
    if (heading && !heading.hasAttribute("tabindex")) heading.tabIndex = -1;
    MissionAttention.restoreFocus(
      previous, document, [heading, document.querySelector(".nav.active")],
    );
  }
}

function showAttention(runCount, questionCount) {
  const element = $("operatorAttention");
  if (element.hidden) {
    const active = document.activeElement;
    state.attentionReturnFocus = active && active !== document.body ? active : null;
  }
  const message = MissionAttention.messageFor(runCount, questionCount);
  $("operatorAttentionTitle").textContent = runCount === 1 ? "Operator attention required" : "Multiple investigations need attention";
  $("operatorAttentionMessage").textContent = message;
  element.hidden = false;
  element.classList.remove("show");
  cancelAnimationFrame(showAttention.frame);
  clearTimeout(showAttention.announcementTimer);
  $("operatorAttentionAnnouncer").textContent = "";
  showAttention.frame = requestAnimationFrame(() => {
    element.classList.add("show");
    showAttention.announcementTimer = setTimeout(() => {
      $("operatorAttentionAnnouncer").textContent = message;
    }, 40);
  });
}

async function announceAttention(transitions) {
  const claimed = [];
  for (const run of transitions) {
    try {
      const snapshot = await api(`/api/runs/${encodeURIComponent(run.runId)}`);
      const questionCount = MissionAttention.claimQuestionState(
        state.attention, run.runId, snapshot.pendingQuestions || [],
      );
      if (questionCount) claimed.push(questionCount);
    } catch (_error) {
      state.attention.rearm(run.runId);
    }
  }
  if (claimed.length) showAttention(claimed.length, claimed.reduce((total, count) => total + count, 0));
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function setRestartState(restarting) {
  state.restarting = restarting;
  document.body.classList.toggle("service-restarting", restarting);
  document.querySelectorAll("#restartService, [data-restart-service]").forEach(button => {
    button.disabled = restarting || state.config?.restartAvailable === false;
    button.innerHTML = restarting ? `<span class="restart-spinner" aria-hidden="true">${REFRESH_ICON}</span> Restarting…` : `<span aria-hidden="true">${REFRESH_ICON}</span> ${button.id === "restartService" ? "Restart UI" : "Restart UI service"}`;
  });
  if (restarting) $("healthText").textContent = "restarting service";
}

async function restartService() {
  if (state.restarting) return;
  const previousInstance = state.config?.serviceInstance;
  setRestartState(true);
  if (state.stream) state.stream.close();
  try {
    await api("/api/restart", {method: "POST", body: "{}"});
    toast("Factory is restarting…");
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      await delay(300);
      try {
        const response = await fetch(`/api/config?restart=${Date.now()}`, {cache: "no-store"});
        if (!response.ok) continue;
        const config = await response.json();
        if (config.serviceInstance && config.serviceInstance !== previousInstance) {
          window.location.reload();
          return;
        }
      } catch (_error) { /* Downtime is expected while the process is replaced. */ }
    }
    throw new Error("Factory did not return within 20 seconds");
  } catch (error) {
    setRestartState(false);
    toast(error.message, true);
    refreshFleet();
  }
}

function show(name) {
  const generation = ++state.viewGeneration;
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav").forEach(nav => nav.classList.toggle("active", nav.dataset.view === name));
  $("crumb").textContent = name.replaceAll("-", " ").toUpperCase();
  return {generation, ready: name === "inbox" ? loadInbox() : name === "settings" ? Promise.all([loadNotificationConfiguration(), loadNotifications()]) : Promise.resolve()};
}

function focusInbox() {
  MissionAttention.focusInbox($("inbox"), $("inboxHeading"));
}

function openAttentionInbox() {
  dismissAttention({restoreFocus: false});
  const navigation = show("inbox");
  navigation.ready.then(() => {
    if (MissionAttention.shouldFocusInbox(
      navigation.generation, state.viewGeneration, $("view-inbox"),
    )) focusInbox();
  });
}

function activateDetailTab(tab, {focus = false} = {}) {
  if (!tab) return false;
  const tablist = tab.closest('[role="tablist"]');
  tablist.querySelectorAll('[role="tab"]').forEach(item => {
    const active = item === tab;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
    item.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("#view-detail .tabpane").forEach(item => item.classList.toggle("active", item.id === `tab-${tab.dataset.tab}`));
  if (focus) tab.focus({preventScroll: true});
  return true;
}

function activate(element) {
  if (element.closest("#themeToggle")) { toggleTheme(); return true; }
  const restart = element.closest("#restartService, [data-restart-service]");
  if (restart) { restartService(); return true; }
  if (element.closest("[data-notification-refresh]")) { loadNotifications(); return true; }
  if (element.closest("[data-notification-config-save]")) { saveNotificationConfiguration(); return true; }
  const channelTest = element.closest("[data-notification-channel-test]");
  if (channelTest) { testNotificationChannel(channelTest); return true; }
  const preview = element.closest("[data-notification-preview]");
  if (preview) { previewNotification(preview); return true; }
  const acknowledgement = element.closest("[data-notification-ack]");
  if (acknowledgement) { acknowledgeNotification(acknowledgement); return true; }
  const notificationLink = element.closest("[data-notification-link]");
  if (notificationLink) { openNotificationLink(notificationLink); return true; }
  const view = element.closest("[data-view]");
  if (view) { show(view.dataset.view); return true; }
  if (element.closest("[data-attention-open]")) { openAttentionInbox(); return true; }
  if (element.closest("[data-attention-dismiss], [data-attention-later]")) { dismissAttention(); return true; }
  if (element.closest("[data-campaign-close]")) { closeCampaign(); return true; }
  const changeDecision = element.closest("[data-campaign-change-decision]");
  if (changeDecision) { decideCampaignChange(changeDecision); return true; }
  const campaignAction = element.closest("[data-campaign-action]");
  if (campaignAction) { transitionCampaign(campaignAction.dataset.campaignId, campaignAction.dataset.campaignAction); return true; }
  const campaignLink = element.closest("[data-campaign-link]");
  if (campaignLink) { openCampaignLink(campaignLink); return true; }
  const campaignCopy = element.closest("[data-campaign-copy]");
  if (campaignCopy) { copyText(campaignCopy.dataset.campaignCopy, "Canonical record ID copied."); return true; }
  const campaign = element.closest("[data-campaign]");
  if (campaign) { openCampaign(campaign.dataset.campaign); return true; }
  const memoryToggle = element.closest("[data-memory-toggle]");
  if (memoryToggle) {
    const body = document.getElementById(memoryToggle.getAttribute("aria-controls"));
    const expanded = memoryToggle.getAttribute("aria-expanded") !== "false";
    memoryToggle.setAttribute("aria-expanded", String(!expanded));
    if (body) body.hidden = expanded;
    return true;
  }
  const knowledgeToggle = element.closest("[data-knowledge-toggle]");
  if (knowledgeToggle) {
    const body = document.getElementById(knowledgeToggle.getAttribute("aria-controls"));
    const expanded = knowledgeToggle.getAttribute("aria-expanded") === "true";
    knowledgeToggle.setAttribute("aria-expanded", String(!expanded));
    if (body) body.hidden = expanded;
    return true;
  }
  const knowledgeCopy = element.closest("[data-knowledge-copy]");
  if (knowledgeCopy) { copyText(knowledgeCopy.dataset.knowledgeCopy, "Content hash copied."); return true; }
  const outcomeTab = element.closest("[data-outcome-tab]");
  if (outcomeTab) return activateDetailTab($(`tab-button-${outcomeTab.dataset.outcomeTab}`), {focus: true});
  const outcomeParent = element.closest("[data-outcome-parent]");
  if (outcomeParent) {
    state.outcomes.filters.query = outcomeParent.dataset.outcomeParent;
    $("outcomeSearch").value = state.outcomes.filters.query;
    renderOutcomeProjection();
    $("outcomeSearch").focus({preventScroll: true});
    return true;
  }
  const researchOutcome = element.closest("[data-research-outcome]");
  if (researchOutcome) { openResearchReference("finding", researchOutcome.dataset.researchOutcome); return true; }
  const memoryOperation = element.closest("[data-memory-operation]");
  if (memoryOperation) { applyProjectMemoryOperation(memoryOperation); return true; }
  const researchReference = element.closest("[data-research-ref-kind]");
  if (researchReference) { openResearchReference(researchReference.dataset.researchRefKind, researchReference.dataset.researchRefId); return true; }
  if (element.closest("#copyMemoryContext")) { copyMemoryContext(); return true; }
  const evidenceAction = element.closest("[data-evidence-action]");
  if (evidenceAction) { updateEvidence(evidenceAction.dataset.evidenceId, evidenceAction.dataset.evidenceAction); return true; }
  const card = element.closest("[data-run]");
  if (card) { openRun(card.dataset.run); return true; }
  const answer = element.closest("[data-answer]");
  if (answer) { resolveDecision(answer.dataset.run, answer.dataset.question, answer.dataset.answer); return true; }
  const direction = element.closest("[data-direction-submit]");
  if (direction) {
    const input = direction.closest(".decision").querySelector("[data-direction-input]");
    const value = input.value.trim();
    if (!value) { input.focus(); toast("Add direction before sending", true); return true; }
    resolveDecision(direction.dataset.run, direction.dataset.question, value);
    return true;
  }
  const tab = element.closest("[data-tab]");
  if (tab) return activateDetailTab(tab);
  return false;
}

document.addEventListener("click", event => activate(event.target));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("campaignDialog").open) { event.preventDefault(); closeCampaign(); return; }
  if (event.key === "Escape" && !$("operatorAttention").hidden) { event.preventDefault(); dismissAttention(); }
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-run]")) {
    event.preventDefault();
    activate(event.target);
  }
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-campaign]")) {
    event.preventDefault(); openCampaign(event.target.dataset.campaign);
  }
  if (event.target.matches('[role="tab"]') && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    const tabs = [...event.target.closest('[role="tablist"]').querySelectorAll('[role="tab"]')];
    let index = tabs.indexOf(event.target);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = tabs.length - 1;
    else index = (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault(); tabs[index].focus(); activate(tabs[index]);
  }
  if (event.key === "/" && $("view-detail").classList.contains("active") && $("tab-outcomes").classList.contains("active") && !event.target.matches("input, textarea, select")) {
    event.preventDefault(); $("outcomeSearch").focus();
  }
});

document.addEventListener("input", event => {
  if (event.target.id !== "outcomeSearch") return;
  delete state.outcomes.filters.exactId;
  state.outcomes.filters.query = event.target.value;
  renderOutcomeProjection();
});

document.addEventListener("change", event => {
  const names = {outcomeType: "type", outcomeState: "state", outcomeOwner: "owner", outcomeTerminal: "terminal"};
  const name = names[event.target.id];
  if (!name) return;
  delete state.outcomes.filters.exactId;
  state.outcomes.filters[name] = event.target.value;
  renderOutcomeProjection();
});

document.addEventListener("reset", event => {
  if (event.target.id !== "outcomeFilters") return;
  state.outcomes.filters = {query: "", type: "all", state: "all", owner: "all", terminal: "all"};
  requestAnimationFrame(() => renderOutcomeProjection());
});

function statusRank(status) {
  return ({needs_input: 0, running: 1, queued: 2, failed: 3, completed: 4, blocked: 5})[status] ?? 9;
}

function latestStep(run) {
  const active = run.workers.find(worker => worker.status === "running") || run.workers.find(worker => worker.current_step);
  return active?.current_step || run.latestEvent?.message || "awaiting work";
}

function elapsedLabel(run, now = Date.now()) {
  const start = Date.parse(run.createdAt), terminal = ["completed", "partial", "failed", "blocked", "canceled"].includes(run.status);
  const end = terminal ? Date.parse(run.completedAt || run.updatedAt) : now;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return run.status === "queued" ? "queued" : "time unavailable";
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

const TARGET_ICONS = {
  TREE: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7h7l2 2h9v10H3z"/><path d="M7 13h10M7 16h7"/></svg>`,
  PE: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/></svg>`,
  APK: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4M9 5h6"/></svg>`,
  JAR: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M8 3h8M9 3v4l-3 4v8h12v-8l-3-4V3"/><path d="M7 13h10"/></svg>`,
  ELF: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 12h7M9 16h7"/></svg>`,
  BIN: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9v6M11 9h3v6h-3zM17 9v6"/></svg>`,
};

function targetKind(target) {
  const name = target.split("/").pop() || "target", suffix = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  const known = {exe: "PE", dll: "PE", apk: "APK", ipa: "IPA", asar: "ASAR", zip: "ZIP", jar: "JAR", wasm: "WASM", elf: "ELF", dylib: "MACH"};
  const label = known[suffix] || (suffix ? suffix.slice(0, 4).toUpperCase() : "TREE");
  return {label, icon: TARGET_ICONS[label] || TARGET_ICONS.BIN};
}

function renderCampaigns() {
  const campaigns = state.campaigns;
  const needs = campaigns.filter(MissionCampaigns.needsAction).length;
  const active = campaigns.filter(campaign => ["running", "waiting", "suspended"].includes(campaign.status)).length;
  const degraded = campaigns.filter(campaign => campaign.health?.degraded === true).length;
  $("campaignSynthesis").innerHTML = MissionCampaigns.renderSynthesis(campaigns);
  $("campaignFleet").innerHTML = campaigns.length ? campaigns.map(MissionCampaigns.renderCard).join("") : `<div class="empty compact"><b>No bounded campaigns</b>Campaign health will appear when a multi-epoch controller publishes canonical state.</div>`;
  $("campaignBoardHealth").innerHTML = `<span class="live"><i></i>${active} active</span><span class="${needs ? "attention" : ""}"><b>${needs}</b> need you</span><span class="${degraded ? "attention" : ""}"><b>${degraded}</b> degraded</span><span><b>${campaigns.length}</b> total</span>`;
}

async function refreshCampaigns() {
  const generation = ++state.campaignListRequest;
  try {
    const payload = await api("/api/campaigns");
    if (generation !== state.campaignListRequest) return;
    state.campaigns = Array.isArray(payload.campaigns) ? payload.campaigns : [];
    renderCampaigns();
    if (state.campaignSelected && $("campaignDialog").open && !state.campaignAction) {
      const current = state.campaigns.find(item => item.campaignId === state.campaignSelected);
      if (current) await refreshOpenCampaign(current.campaignId);
      else closeCampaign();
    }
  } catch (error) {
    if (generation !== state.campaignListRequest) return;
    $("campaignBoardHealth").innerHTML = `<span class="attention"><b>!</b> projection unavailable</span>`;
    $("campaignSynthesis").innerHTML = MissionCampaigns.renderSynthesis([]);
    $("campaignFleet").innerHTML = `<div class="empty compact"><b>Campaign projection unavailable</b>Mission Control will retry the bounded read model automatically.</div>`;
  }
}

async function refreshOpenCampaign(campaignId) {
  const generation = ++state.campaignDetailRequest;
  try {
    const payload = await api(`/api/campaigns/${encodeURIComponent(campaignId)}`);
    if (generation !== state.campaignDetailRequest || state.campaignSelected !== campaignId || !$("campaignDialog").open) return;
    const campaign = payload.campaign;
    if (!campaign || campaign.campaignId !== campaignId) throw new Error("Campaign projection identity mismatch");
    state.campaigns = state.campaigns.map(item => item.campaignId === campaignId ? campaign : item);
    $("campaignDetail").innerHTML = MissionCampaigns.renderDetail(campaign);
  } catch (_error) {
    if (generation !== state.campaignDetailRequest || state.campaignSelected !== campaignId || !$("campaignDialog").open) return;
    const bounded = state.campaigns.find(item => item.campaignId === campaignId);
    if (bounded) $("campaignDetail").innerHTML = MissionCampaigns.renderDetail(bounded);
    toast("Campaign record links are unavailable; stale links were removed.", true);
  }
}

async function openCampaign(campaignId) {
  const generation = ++state.campaignDetailRequest;
  state.campaignReturnFocus = document.activeElement?.closest?.("[data-campaign]") || null;
  try {
    const payload = await api(`/api/campaigns/${encodeURIComponent(campaignId)}`);
    if (generation !== state.campaignDetailRequest) return;
    const campaign = payload.campaign;
    if (!campaign || campaign.campaignId !== campaignId) throw new Error("Campaign projection identity mismatch");
    state.campaignSelected = campaignId;
    state.campaigns = state.campaigns.some(item => item.campaignId === campaignId) ? state.campaigns.map(item => item.campaignId === campaignId ? campaign : item) : [...state.campaigns, campaign];
    $("campaignDetail").innerHTML = MissionCampaigns.renderDetail(campaign);
    if (!$("campaignDialog").open) $("campaignDialog").showModal();
  } catch (_error) { if (generation === state.campaignDetailRequest) toast("Campaign detail is unavailable; canonical state will be retried.", true); }
}

function closeCampaign() {
  state.campaignSelected = null;
  if ($("campaignDialog").open) $("campaignDialog").close();
  const previous = state.campaignReturnFocus;
  state.campaignReturnFocus = null;
  if (previous?.isConnected) previous.focus({preventScroll: true});
}

function campaignOperationId(campaignId, action, revision) {
  // Exact canonical revision makes this stable across reloads and repeated clicks.
  return {key: null, value: `mission-control:${campaignId}:${action}:${revision}`};
}

async function transitionCampaign(campaignId, action) {
  if (state.campaignAction) return;
  const campaign = state.campaigns.find(item => item.campaignId === campaignId);
  if (!campaign || !MissionCampaigns.canonicalActions(campaign).includes(action)) {
    toast("That transition is no longer authorized; refreshing canonical state.", true);
    await refreshCampaigns(); return;
  }
  const prompt = action === "stop" ? "Stop this bounded campaign? This terminal transition cannot be undone." : `${action === "pause" ? "Pause" : "Resume"} this bounded campaign?`;
  if (!window.confirm(prompt)) return;
  const operation = campaignOperationId(campaignId, action, campaign.revision);
  state.campaignAction = `${campaignId}:${action}`;
  $("campaignDialog").setAttribute("aria-busy", "true");
  document.querySelectorAll("[data-campaign-action]").forEach(button => { button.disabled = true; });
  try {
    const body = {operationId: operation.value, expectedRevision: campaign.revision};
    if (action === "stop") Object.assign(body, {reasonCode: "operator-stop", evidenceIds: []});
    const payload = await api(`/api/campaigns/${encodeURIComponent(campaignId)}/${action}`, {method: "POST", body: JSON.stringify(body)});
    if (!payload.campaign || payload.campaign.campaignId !== campaignId) throw new Error("Campaign transition returned mismatched state");
    if (operation.key) sessionStorage.removeItem(operation.key);
    state.campaigns = state.campaigns.map(item => item.campaignId === campaignId ? payload.campaign : item);
    $("campaignDetail").innerHTML = MissionCampaigns.renderDetail(payload.campaign);
    renderCampaigns(); toast(`Campaign ${action} recorded.`);
  } catch (_error) {
    toast("Campaign transition was not accepted. Canonical state has been refreshed.", true);
    await refreshCampaigns();
  } finally { state.campaignAction = null; $("campaignDialog").setAttribute("aria-busy", "false"); }
}

async function decideCampaignChange(button) {
  if (state.campaignAction) return;
  const campaignId = button.dataset.campaignId, requestId = button.dataset.changeRequest;
  const expectedRevision = Number(button.dataset.changeRevision);
  const approved = button.dataset.campaignChangeDecision === "approve";
  const campaign = state.campaigns.find(item => item.campaignId === campaignId);
  const decision = campaign && MissionCampaigns.changeDecisionPayload(campaign, requestId, approved);
  if (!decision || decision.expectedRevision !== expectedRevision) {
    toast("That campaign request is no longer pending; refreshing canonical state.", true);
    if (campaignId) await refreshOpenCampaign(campaignId);
    $("campaignDialog").querySelector("[data-campaign-close]")?.focus({preventScroll: true});
    return;
  }
  if (!window.confirm(`${approved ? "Approve" : "Reject"} this exact server-published campaign change?`)) return;
  state.campaignAction = `${campaignId}:change:${requestId}`;
  $("campaignDialog").setAttribute("aria-busy", "true");
  document.querySelectorAll("[data-campaign-change-decision], [data-campaign-action]").forEach(control => { control.disabled = true; });
  try {
    const payload = await api(`/api/campaigns/${encodeURIComponent(campaignId)}/change-decisions`, {
      method: "POST", body: JSON.stringify(decision),
    });
    if (!payload.changeRequest || payload.changeRequest.requestId !== requestId) throw new Error("Campaign change response identity mismatch");
    if (approved && (typeof payload.approvedCampaignId !== "string" || !payload.approvedCampaignId)) throw new Error("Approved campaign identity unavailable");
    if (!approved && payload.approvedCampaignId !== null) throw new Error("Rejected request returned an approved campaign identity");
    if (approved) closeCampaign();
    await refreshCampaigns();
    if (approved && state.campaigns.some(item => item.campaignId === payload.approvedCampaignId)) await openCampaign(payload.approvedCampaignId);
    else if (!approved && state.campaignSelected === campaignId) {
      await refreshOpenCampaign(campaignId);
      $("campaignDialog").querySelector("[data-campaign-close]")?.focus({preventScroll: true});
    }
    toast(`Campaign change ${approved ? "approved" : "rejected"}.`);
  } catch (_error) {
    toast("Campaign change decision was not accepted. Canonical state has been refreshed.", true);
    if (state.campaignSelected === campaignId) await refreshOpenCampaign(campaignId);
    else await refreshCampaigns();
    const retry = $("campaignDialog").querySelector(`[data-campaign-change="${CSS.escape(requestId)}"] [data-campaign-change-decision="${approved ? "approve" : "reject"}"]`);
    (retry || $("campaignDialog").querySelector("[data-campaign-close]"))?.focus({preventScroll: true});
  } finally {
    state.campaignAction = null;
    $("campaignDialog").setAttribute("aria-busy", "false");
    document.querySelectorAll("[data-campaign-change-decision], [data-campaign-action]").forEach(control => { control.disabled = false; });
  }
}

async function openCampaignLink(link) {
  const surface = link.dataset.campaignLink;
  const runId = link.dataset.campaignRun || (surface === "activity" ? link.dataset.campaignRef : null);
  if (!runId) { toast("No bounded run link is available for this reference.", true); return; }
  const kind = link.dataset.campaignKind, entityId = link.dataset.campaignRef;
  closeCampaign();
  await openRun(runId);
  if (state.selected !== runId || state.snapshot?.run?.id !== runId) return;
  const tab = ["artifacts", "outcomes", "dossiers", "activity"].includes(surface) ? surface : "activity";
  if (tab === "outcomes") {
    state.outcomes.filters = {query: entityId, exactId: entityId, type: kind, state: "all", owner: "all", terminal: "all"};
    $("outcomeSearch").value = entityId;
    renderOutcomeProjection();
  }
  activateDetailTab($(`tab-button-${tab}`), {focus: true});
  const exact = tab === "artifacts" ? document.querySelector(`[data-evidence-record="${CSS.escape(entityId)}"]`)
    : tab === "dossiers" ? document.querySelector(`[data-dossier-id="${CSS.escape(entityId)}"]`)
      : tab === "outcomes" ? document.querySelector(`[data-outcome-id="${CSS.escape(entityId)}"][data-outcome-type="${CSS.escape(kind)}"]`) : null;
  if (exact) { exact.classList.add("campaign-linked-target"); exact.scrollIntoView({block: "center"}); exact.focus({preventScroll: true}); }
  else if (tab !== "activity") toast("The canonical record is no longer present in this run.", true);
}

function renderFleet() {
  const runs = [...state.fleet].sort((left, right) => statusRank(left.status) - statusRank(right.status));
  const counts = {all: runs.length};
  runs.forEach(run => { counts[run.status] = (counts[run.status] || 0) + 1; });
  $("filters").innerHTML = ["all", "needs_input", "running", "queued", "failed", "completed"].map(key =>
    `<button class="chip ${state.filter === key ? "active" : ""}" data-filter="${key}">${key.replace("_", " ")} · ${counts[key] || 0}</button>`
  ).join("");
  document.querySelectorAll("[data-filter]").forEach(button => { button.onclick = () => { state.filter = button.dataset.filter; renderFleet(); }; });
  const query = state.query.trim().toLowerCase();
  const shown = runs.filter(run => (state.filter === "all" || run.status === state.filter) && (!query || [
    run.target, run.goal, run.projectId, run.status, run.modelProfile?.model, latestStep(run),
  ].some(value => String(value || "").toLowerCase().includes(query))));
  $("fleet").innerHTML = shown.length ? shown.map(run => {
    const kind = targetKind(run.target), total = Math.max(0, numeric(run.coverage.workItemsTotal));
    const terminal = Math.max(0, numeric(run.coverage.terminal));
    const progress = total ? Math.min(100, Math.round(terminal / total * 100)) : 0;
    return `
    <article class="card ${esc(run.status)}" data-run="${esc(run.runId)}" tabindex="0" role="button" aria-label="Open ${esc(run.target.split("/").pop())}">
      <div class="card-top"><div class="target-kind" aria-hidden="true"><i>${kind.icon}</i><span>${esc(kind.label)}</span></div><div class="target-block"><div class="target">${esc(run.target.split("/").pop())}</div><small>${esc(run.projectId)}</small></div><div class="pill ${esc(run.status)}">${esc(run.status.replace("_", " "))}</div></div>
      <div class="goal">${esc(run.goal)}</div><div class="step">${esc(latestStep(run))}</div>
      <div class="card-meta"><span class="tag"><strong>${esc(run.modelProfile.model)}</strong></span><span class="tag">${run.workers.length} workers</span><span class="tag">pass ${numeric(run.iteration)} / ${numeric(run.maxIterations)}</span>${run.needsYou ? `<span class="tag warn">▲ ${run.needsYou} needs you</span>` : ""}</div>
      <div class="counts"><div class="count"><b>${run.coverage.done}</b><span>done</span></div><div class="count"><b>${run.coverage.pending}</b><span>pending</span></div><div class="count"><b>${run.coverage.failed}</b><span>failed</span></div></div>
      <div class="card-foot"><span>${terminal}/${total} terminal</span><div class="coverage-track" role="progressbar" aria-label="Run coverage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><i style="width:${progress}%"></i></div><time>${esc(elapsedLabel(run))}</time></div>${run.needsYou ? `<div class="attention-cue"><span>Operator decision waiting</span><b>Open investigation →</b></div>` : ""}
    </article>`;
  }).join("") : `<div class="empty"><b>${query ? "No matching investigations" : "No investigations here"}</b>${query ? "Try a target, goal, model, status, or current step." : "Launch a run or change the filter."}</div>`;
  const running = runs.filter(run => run.status === "running").length;
  const needs = runs.reduce((total, run) => total + run.needsYou, 0);
  const terminal = runs.reduce((total, run) => total + numeric(run.coverage.terminal), 0);
  const workItems = runs.reduce((total, run) => total + numeric(run.coverage.workItemsTotal), 0);
  const models = new Set(runs.map(run => run.modelProfile?.model).filter(Boolean));
  $("fleetHealth").innerHTML = `<span class="live"><i></i>${running} active</span><span><b>${terminal}/${workItems}</b> work terminal</span><span><b>${models.size}</b> models</span><span class="${needs ? "attention" : ""}"><b>${needs}</b> decisions waiting</span><span class="fleet-health-spacer"></span><span>${shown.length} shown</span>`;
  $("fleetSub").textContent = `${runs.length} investigations · ${running} running · ${needs} awaiting your call`;
  $("stats").innerHTML = `<span class="stat"><b>${runs.length}</b> runs</span><span class="stat"><b>${runs.reduce((total, run) => total + run.workers.length, 0)}</b> workers</span>`;
  $("healthText").textContent = `${running} active · local`;
  $("inboxBadge").textContent = needs;
  $("inboxBadge").style.display = needs ? "grid" : "none";
}

async function refreshFleet() {
  try {
    const runs = (await api("/api/fleet")).runs;
    const transitions = state.attention.transitions(runs);
    state.fleet = runs;
    renderFleet();
    if (transitions.length) await announceAttention(transitions);
  }
  catch (error) { $("healthText").textContent = "API unavailable"; toast(error.message, true); }
}

function decisionHTML(runId, question) {
  return MissionObservability.renderDecision(runId, question);
}

async function loadInbox() {
  const pending = [];
  for (const run of state.fleet.filter(item => item.needsYou)) {
    try { const snapshot = await api(`/api/runs/${encodeURIComponent(run.runId)}`); snapshot.pendingQuestions.forEach(question => pending.push([run.runId, question])); }
    catch (_error) { /* A disappearing run is reconciled on the next fleet refresh. */ }
  }
  $("inbox").innerHTML = pending.length ? pending.map(([runId, question]) => decisionHTML(runId, question)).join("") : `<div class="empty"><b>All clear</b>No investigation needs your decision.</div>`;
}

async function resolveDecision(runId, questionId, answer) {
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/answers`, {method: "POST", body: JSON.stringify({questionId, answer})});
    toast(`Decision recorded: ${answer}`); await refreshFleet();
    if (state.selected === runId) await openRun(runId); else loadInbox();
  } catch (error) { toast(error.message, true); }
}

async function openRun(runId) {
  const requestGeneration = state.runRequests.begin();
  state.snapshotRefreshes.invalidate();
  try {
    state.outcomes.tracker.reset();
    state.outcomes.projection = null;
    state.outcomes.integrity = "missing";
    state.outcomes.filters = {query: "", type: "all", state: "all", owner: "all", terminal: "all"};
    state.selected = runId;
    state.snapshot = null;
    state.evidence = [];
    const [snapshot, reportPayload, evidence, dossierPayload] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}`),
      api(`/api/runs/${encodeURIComponent(runId)}/reports`),
      api(`/api/runs/${encodeURIComponent(runId)}/evidence`),
      api(`/api/runs/${encodeURIComponent(runId)}/dossiers`),
    ]);
    if (!state.runRequests.isCurrent(requestGeneration) || state.selected !== runId) return;
    snapshot.workerReports = reportPayload.reports;
    snapshot.dossiers = dossierPayload.dossiers;
    state.snapshot = snapshot; state.evidence = evidence.records || [];
    const snapshotCursor = MissionOutcomes.latestEventId(snapshot.events);
    if (snapshotCursor) state.streamCursors.set(runId, snapshotCursor);
    else state.streamCursors.delete(runId);
    renderDetail(); show("detail"); connectEvents(runId);
  }
  catch (error) {
    if (state.runRequests.isCurrent(requestGeneration) && state.selected === runId) {
      toast(error.message, true);
    }
  }
}

async function updateEvidence(artifactId, action) {
  if (!state.selected) return;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(state.selected)}/evidence/${encodeURIComponent(artifactId)}/${action}`, {method: "POST", body: JSON.stringify({citationId: `operator:${state.selected}`})});
    state.evidence = state.evidence.map(item => item.artifactId === artifactId ? result.record : item);
    renderEvidence();
    toast(result.event.action === "retention_conflict" ? "Retention conflict: remove the pin or hold before deleting." : `Evidence ${action} recorded.`);
  } catch (error) { toast(error.message, true); }
}

function renderEvidence() {
  const records = state.evidence || [];
  $("evidenceLifecycle").innerHTML = records.length ? records.map(record => {
    const pinned = (record.citations || []).includes(`operator:${record.runId}`), inactive = ["deleted", "expired"].includes(record.state);
    const labels = record.quarantineLabels?.length ? `<span class="evidence-warning">restricted: ${esc(record.quarantineLabels.join(", "))}</span>` : "";
    return `<article class="evidence-card ${esc(record.state)}" data-evidence-record="${esc(record.artifactId)}" tabindex="-1"><div class="evidence-head"><div><b>${esc(record.kind)}</b><span>${esc(record.state.replaceAll("_", " "))}</span></div><code>${esc(record.originalSha256.slice(0, 12))}</code></div><div class="evidence-facts"><span>${esc(record.mediaType)}</span><span>${record.rawSize.toLocaleString()} bytes</span><span>${esc(record.retentionClass)}</span>${record.redacted ? "<span>redacted projection</span>" : ""}${record.expiresAt ? `<span>expires ${esc(new Date(record.expiresAt).toLocaleString())}</span>` : ""}${labels}</div><div class="evidence-actions"><button class="btn" data-evidence-id="${esc(record.artifactId)}" data-evidence-action="${pinned ? "unpin" : "pin"}" ${inactive ? "disabled" : ""}>${pinned ? "Unpin" : "Pin"}</button><button class="btn" data-evidence-id="${esc(record.artifactId)}" data-evidence-action="${record.held ? "unhold" : "hold"}" ${inactive ? "disabled" : ""}>${record.held ? "Release hold" : "Hold"}</button><button class="btn red" data-evidence-id="${esc(record.artifactId)}" data-evidence-action="delete" ${inactive ? "disabled" : ""}>Delete</button></div></article>`;
  }).join("") : `<div class="empty compact"><b>No retained evidence</b>Proof-required captures and their lifecycle state will appear here.</div>`;
}

const memoryValues = value => Array.isArray(value) ? value : value && typeof value === "object" ? Object.values(value) : [];
const memoryStatus = item => String(item.status || "recorded").replaceAll("_", " ");
const memoryTone = status => ({active: "live", running: "live", supported: "good", completed: "good", answered: "good", failed: "bad", blocked: "bad", disproven: "bad", rejected: "bad", inconclusive: "warn", paused: "warn", testing: "testing", open: "testing", pending: "testing"})[status] || "neutral";
const memoryRefs = item => (item.references || []).map(reference => `${reference.kind}:${reference.id}`);

function workstreamStopAuthority(item) {
  const authority = state.snapshot?.memoryAuthority;
  if (!authority || authority.schemaVersion !== 1 || authority.degraded !== false
      || authority.projectId !== state.snapshot?.meta?.projectId
      || !Number.isInteger(authority.revision) || authority.revision < 0
      || !Array.isArray(authority.operations)) return null;
  const operation = authority.operations.find(value => value?.action === "workstream-stop"
    && value.entityType === "workstream" && value.entityId === item.id
    && typeof value.expectedEntitySha256 === "string"
    && /^[0-9a-f]{64}$/.test(value.expectedEntitySha256));
  return operation ? {...operation, expectedProjectId: authority.projectId,
    expectedRevision: authority.revision} : null;
}

function memoryCard(item, group, index) {
  const fields = {
    workstreams: [item.title, item.goal, item.nextAction && `Next: ${item.nextAction}`, item.stopCondition && `Stop when: ${item.stopCondition}`],
    attempts: [item.method, item.intent, item.result, item.followUp && `Follow-up: ${item.followUp}`],
    decisions: [item.choice, item.rationale, item.alternatives?.length && `Alternatives: ${item.alternatives.join(" · ")}`],
    questions: [item.question],
    theories: [item.claim, item.confidence !== undefined && `Confidence: ${typeof item.confidence === "number" ? Math.round(item.confidence * 100) + "%" : item.confidence}`],
    next_actions: [item.text, item.blockers?.length && `Blocked by: ${item.blockers.join(", ")}`],
  }[group] || [item.text || item.title || item.id];
  const values = fields.filter(Boolean), title = values.shift() || item.id || "Memory record";
  const refs = memoryRefs(item), status = String(item.status || "recorded");
  const stop = group === "workstreams" ? workstreamStopAuthority(item) : null;
  const control = stop ? `<div class="memory-authority-controls"><button class="btn red" type="button" data-memory-operation="workstream-stop" data-memory-entity="${esc(item.id)}" data-memory-project="${esc(stop.expectedProjectId)}" data-memory-revision="${stop.expectedRevision}" data-memory-digest="${esc(stop.expectedEntitySha256)}">Stop workstream</button></div>` : "";
  return `<article class="memory-card ${memoryTone(status)}" style="--memory-order:${index}"><header><span>${esc(group.replaceAll("_", " "))}</span><b>${esc(memoryStatus(item))}</b></header><h4>${esc(title)}</h4>${values.map(value => `<p>${esc(value)}</p>`).join("")}${refs.length ? `<footer>${refs.map(reference => `<code>${esc(reference)}</code>`).join("")}</footer>` : ""}${control}<i class="memory-seq">#${esc(item._eventSeq ?? "—")}</i></article>`;
}

async function applyProjectMemoryOperation(button) {
  if (!state.selected || button.disabled) return;
  const action = button.dataset.memoryOperation, entityId = button.dataset.memoryEntity;
  const expectedRevision = Number(button.dataset.memoryRevision);
  const expectedEntitySha256 = button.dataset.memoryDigest;
  const expectedProjectId = button.dataset.memoryProject;
  const allowed = state.snapshot?.memoryAuthority?.operations?.some(value =>
    value.action === action && value.entityId === entityId
    && value.expectedEntitySha256 === expectedEntitySha256)
    && state.snapshot.memoryAuthority.revision === expectedRevision
    && state.snapshot.memoryAuthority.projectId === expectedProjectId
    && state.snapshot.meta?.projectId === expectedProjectId;
  if (!allowed) { toast("That exact project-memory authority is stale; refreshing.", true); await openRun(state.selected); return; }
  const verb = action === "workstream-stop" ? "stop this workstream"
    : action === "finding-accept" ? "accept this technically reproduced finding"
      : "reject this finding";
  const rationale = window.prompt(`Give a durable rationale to ${verb}:`, "");
  if (rationale === null) return;
  if (!rationale.trim() || rationale !== rationale.trim() || rationale.length > 2000) {
    toast("Rationale must be 1–2000 characters without leading or trailing whitespace.", true); return;
  }
  button.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(state.selected)}/memory-operations`, {
      method: "POST", body: JSON.stringify({action, entityId, expectedRevision,
        expectedEntitySha256, expectedProjectId, rationale}),
    });
    const runId = state.selected;
    await openRun(runId);
    toast("Durable project-memory decision recorded.");
  } catch (_error) {
    const runId = state.selected;
    if (runId) await openRun(runId);
    toast("Project-memory decision was not accepted; canonical state was refreshed.", true);
  } finally { if (button.isConnected) button.disabled = false; }
}

function renderMemoryGroup(key, label, items, startIndex) {
  if (!items.length) return "";
  const id = `memory-group-${key}`;
  return `<section class="memory-group"><button class="memory-group-toggle" type="button" data-memory-toggle aria-expanded="true" aria-controls="${id}"><span>${esc(label)}</span><b>${items.length}</b><i aria-hidden="true">⌄</i></button><div class="memory-card-grid" id="${id}">${items.map((item, index) => memoryCard(item, key, startIndex + index)).join("")}</div></section>`;
}

function renderScope(meta) {
  const scope = meta.scope;
  if (!scope) {
    $("scopeSummary").innerHTML = `<div class="scope-empty">No canonical scope projection</div>`;
    return;
  }
  const actions = scope.actions || [], until = scope.validUntil ? new Date(scope.validUntil) : null;
  $("scopeSummary").innerHTML = `<div class="scope-state"><i></i><div><b>${esc(scope.scopeId)}</b><span>revision ${esc(scope.revision)} · ${esc(scope.digest?.slice(0, 12) || "no digest")}</span></div></div><div class="scope-facts"><span><b>network</b>${esc(scope.networkMode)}</span><span><b>data</b>${esc(scope.dataHandling)}</span><span><b>credentials</b>${scope.credentialUse ? "allowed" : "withheld"}</span><span><b>valid until</b>${esc(until && !Number.isNaN(until.valueOf()) ? until.toLocaleString() : "not declared")}</span></div><div class="scope-actions">${actions.map(action => `<span>${esc(action.replaceAll("_", " "))}</span>`).join("") || "<span>no actions</span>"}</div><div class="scope-foot">${(scope.targetRefs || []).length} exact target · ${(scope.endpointRefs || []).length} endpoint grants</div>`;
}

function renderMemory(snapshot) {
  const memory = snapshot.memory || {}, goals = memoryValues(memory.goals);
  const groups = [
    ["workstreams", "Workstreams"], ["attempts", "Attempts & negative results"],
    ["decisions", "Decisions"], ["theories", "Theories"],
    ["questions", "Open questions"], ["next_actions", "Next actions"],
  ];
  const collections = groups.map(([key, label]) => {
    const items = memoryValues(memory[key]);
    return [key, label, key === "questions" ? items.filter(item => item.status === "open") : items];
  });
  const entityCount = collections.reduce((total, [, , items]) => total + items.length, 0);
  const openCount = memoryValues(memory.questions).filter(item => item.status === "open").length;
  const negativeCount = memoryValues(memory.attempts).filter(item => ["failed", "blocked", "inconclusive"].includes(item.status)).length;
  const currentGoal = goals.at(-1)?.text || snapshot.meta.goal || "No durable project goal recorded";
  $("memoryGoal").textContent = currentGoal;
  $("memorySummary").innerHTML = `<span><b>${entityCount}</b> records</span><span><b>${negativeCount}</b> negatives</span><span><b>${openCount}</b> open</span><span class="${memory.degraded ? "degraded" : "healthy"}"><b>${memory.degraded ? "!" : "✓"}</b>${memory.degraded ? "degraded" : "verified fold"}</span>`;
  $("memoryRibbon").classList.toggle("degraded", Boolean(memory.degraded));
  $("memoryCount").textContent = entityCount;
  $("memoryContext").textContent = snapshot.memoryContext || "No bounded resume context has been projected yet.";
  const diagnostics = memoryValues(memory.diagnostics), missing = memoryValues(memory.missing_references);
  const unknowns = memoryValues(memory.compaction?.unknowns);
  const degraded = memory.degraded ? `<div class="memory-alert"><b>Memory projection is degraded</b><span>${diagnostics.map(esc).join(" · ") || "Canonical replay reported a degraded state."}</span>${missing.length ? `<small>${missing.length} optional references are unavailable.</small>` : ""}</div>` : "";
  const unresolved = unknowns.length ? `<div class="memory-unresolved"><b>Unresolved at last compaction</b><span>${unknowns.map(esc).join(" · ")}</span></div>` : "";
  $("memoryDegraded").innerHTML = degraded + unresolved;
  $("researchWorkspace").innerHTML = MissionResearch.render({...snapshot, evidenceRecords: state.evidence});
  let index = 0;
  const rendered = collections.map(([key, label, items]) => {
    const result = renderMemoryGroup(key, label, items, index);
    index += items.length;
    return result;
  }).join("");
  $("memoryGroups").innerHTML = rendered || `<div class="empty compact"><b>No reasoning records yet</b>Durable workstreams, attempts, decisions, theories, questions, and next actions will appear here.</div>`;
}

function openResearchReference(kind, entityId) {
  if (!entityId) return;
  if (["evidence", "artifact"].includes(kind)) {
    activateDetailTab($("tab-button-artifacts"), {focus: true});
    const target = document.querySelector(`[data-evidence-record="${CSS.escape(entityId)}"], [data-artifact-record="${CSS.escape(entityId)}"]`);
    if (target) { target.scrollIntoView({block: "center"}); target.focus({preventScroll: true}); }
    else toast("The exact evidence record is not retained in this run.", true);
    return;
  }
  state.outcomes.filters = {query: entityId, exactId: entityId, type: ["hypothesis", "finding", "proof-bundle", "operator-decision"].includes(kind) ? kind : "all", state: "all", owner: "all", terminal: "all"};
  $("outcomeSearch").value = entityId;
  renderOutcomeProjection();
  activateDetailTab($("tab-button-outcomes"), {focus: true});
}

async function copyMemoryContext() {
  const value = state.snapshot?.memoryContext || "";
  if (!value) { toast("No resume context is available yet.", true); return; }
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      const field = document.createElement("textarea"); field.value = value;
      field.style.cssText = "position:fixed;opacity:0;pointer-events:none";
      document.body.appendChild(field); field.select(); document.execCommand("copy"); field.remove();
    }
    toast("Bounded resume context copied.");
  } catch (_error) { toast("The browser did not allow clipboard access.", true); }
}

async function copyText(value, message) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      const field = document.createElement("textarea"); field.value = value;
      field.style.cssText = "position:fixed;opacity:0;pointer-events:none";
      document.body.appendChild(field); field.select(); document.execCommand("copy"); field.remove();
    }
    toast(message);
  } catch (_error) { toast("The browser did not allow clipboard access.", true); }
}

function knowledgeCalls(snapshot) {
  return (snapshot.workItems || []).filter(item => item.operation === "model-knowledge");
}

function knowledgeCallHTML(item, index) {
  const payload = item.payload || {}, result = item.result || {};
  const operation = payload.knowledgeOperation || result.operation || "retrieve";
  const subject = operation === "search" ? payload.query
    : operation === "follow" ? `${payload.root}:${payload.sourceId} → ${payload.linkTarget}`
    : `${payload.root}:${payload.conceptId}`;
  const description = item.error || result.description || (operation === "search"
    ? `${(result.hits || []).length} bounded result${(result.hits || []).length === 1 ? "" : "s"}`
    : "Selected for the worker's live context.");
  const hits = (result.hits || []).map(hit => `<span class="knowledge-hit"><b>${esc(hit.root)}</b> · ${esc(hit.conceptId)}</span>`).join("");
  return `<article class="knowledge-call ${esc(operation)}" style="--knowledge-order:${index}"><div class="knowledge-operation"><b>${esc(operation)}</b><span>bounded call</span></div><div class="knowledge-call-copy"><code title="${esc(subject || "knowledge request")}">${esc(subject || "knowledge request")}</code><p>${esc(description)}</p>${hits ? `<div class="knowledge-hits">${hits}</div>` : ""}</div><span class="knowledge-state ${esc(item.status)}">${esc(item.status)}</span></article>`;
}

function citationHTML(citation) {
  const value = String(citation || "");
  return /^https?:\/\//i.test(value)
    ? `<a href="${esc(value)}" target="_blank" rel="noreferrer">${esc(value)}</a>`
    : `<code>${esc(value)}</code>`;
}

function knowledgeReferenceHTML(reference, calls, index) {
  const result = calls.map(item => item.result).find(item => item?.contentHash === reference.contentHash) || {};
  const id = `knowledge-reference-${index}`;
  const provenance = reference.provenance || {};
  const route = provenance.operation === "follow"
    ? `${provenance.sourceConceptId || "selected concept"} → ${provenance.linkTarget || reference.conceptId}`
    : "selected from a bounded search result";
  return `<article class="knowledge-reference" style="--knowledge-order:${index}"><button class="knowledge-reference-toggle" type="button" data-knowledge-toggle aria-expanded="false" aria-controls="${id}"><span class="knowledge-root-mark" aria-hidden="true">◇</span><span class="knowledge-reference-title"><b>${esc(result.title || reference.conceptId)}</b><span>${esc(reference.root)} · ${esc(reference.conceptId)}</span></span><i aria-hidden="true">⌄</i></button><div class="knowledge-reference-detail" id="${id}" hidden><p class="knowledge-rationale">${esc(reference.queryRationale)}</p><div class="knowledge-reference-facts"><div><span>provenance</span><code>${esc(provenance.operation || "selected")} · ${esc(route)}</code></div><div><span>content hash</span><code>${esc(reference.contentHash)}</code></div><div><span>retention</span><code>reference metadata only · concept body omitted</code></div></div>${reference.citations?.length ? `<div class="knowledge-citations">${reference.citations.map(citationHTML).join("")}</div>` : ""}<button class="btn knowledge-copy" type="button" data-knowledge-copy="${esc(reference.contentHash)}">Copy hash</button></div></article>`;
}

function renderKnowledge(snapshot) {
  const calls = knowledgeCalls(snapshot), references = snapshot.knowledgeReferences || [];
  const roots = snapshot.meta?.knowledgeRoots || [];
  $("knowledgeRunRoots").innerHTML = roots.length
    ? roots.map(root => `<span>${esc(root)}</span>`).join("")
    : `<span>no roots configured</span>`;
  $("knowledgeFlow").innerHTML = calls.length ? calls.map(knowledgeCallHTML).join("") : `<div class="knowledge-empty"><b>No retrieval calls yet</b>Workers will search configured indexes only when project knowledge is relevant.</div>`;
  $("knowledgeReferences").innerHTML = references.length ? references.map((reference, index) => knowledgeReferenceHTML(reference, calls, index)).join("") : `<div class="knowledge-empty"><b>No selected references</b>Search results are not durable selections until a worker opens or follows one.</div>`;
  $("knowledgeCount").textContent = references.length;
}

function renderDossiers(snapshot) {
  const runId = snapshot.run.id, dossiers = snapshot.dossiers || [];
  $("dossiers").closest(".dossier-stage").classList.toggle("has-stale", dossiers.some(item => !item.verified));
  $("dossierCount").textContent = dossiers.length;
  $("dossiers").innerHTML = dossiers.length ? dossiers.map((dossier, index) => {
    const open = `/api/runs/${encodeURIComponent(runId)}/dossiers/${encodeURIComponent(dossier.id)}`;
    const verified = dossier.verified === true, status = verified ? dossier.verdict : "STALE / INVALID";
    const actions = verified ? `<a class="btn primary" href="${open}" target="_blank" rel="noopener">Open dossier</a><a class="btn" href="${open}/download" download>Export ZIP</a>` : `<span class="btn dossier-disabled" aria-disabled="true">Open unavailable</span><span class="btn dossier-disabled" aria-disabled="true">Republish required</span>`;
    return `<article class="dossier-card ${verified ? "verified" : "stale"}" data-dossier-id="${esc(dossier.id)}" tabindex="-1" style="--dossier-order:${index}"><div class="dossier-card-signal" aria-hidden="true"><span>${verified ? "◆" : "!"}</span><i></i></div><div class="dossier-card-copy"><header><span>${esc(dossier.findingId)}</span><b>${esc(status)}</b></header><h3>Evidence dossier</h3><div class="dossier-facts"><span><small>finding state</small><code>${esc(dossier.findingStateSha256.slice(0, 16))}…</code></span><span><small>manifest</small><code>${esc(dossier.manifestSha256.slice(0, 16))}…</code></span><span><small>verification</small><code>${esc(dossier.verificationStatus)}</code></span></div></div><div class="dossier-actions">${actions}</div></article>`;
  }).join("") : `<div class="knowledge-empty"><b>No proof dossiers published</b>A dossier appears only after every required file is materialized and its anchored bundle verifies.</div>`;
}

const OUTCOME_TYPES = {
  run: ["RUN", "◆"], worker: ["WORKER", "◉"], "work-item": ["WORK", "▤"],
  campaign: ["CAMPAIGN", "◎"], archive: ["ARCHIVE", "▣"],
  hypothesis: ["HYPOTHESIS", "◇"], finding: ["FINDING", "◈"],
  validation: ["VALIDATION", "✓"], "proof-bundle": ["PROOF", "⬡"],
  "operator-decision": ["DECISION", "!"],
};

const outcomeFacetTerminal = facet => facet?.terminal && facet.state !== "not-applicable";
const outcomeRaw = facet => facet && facet.rawState !== null
  && ["string", "number", "boolean"].includes(typeof facet.rawState)
  && String(facet.rawState) !== String(facet.state)
  ? `<span class="outcome-raw">raw ${esc(facet.rawState)}</span>` : "";

function outcomeOption(value, current, label = value) {
  return `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
}

function outcomeCard(entity, index) {
  const [label, icon] = OUTCOME_TYPES[entity.entityType] || [String(entity.entityType || "ENTITY").toUpperCase(), "·"];
  const facets = MissionOutcomes.FACETS.map(name => [name, entity.facets?.[name]]);
  const unknown = facets.some(([, facet]) => facet && (!facet.known || facet.state === "unknown"));
  const diagnostics = entity.diagnostics || [];
  const parent = entity.parent;
  const link = MissionOutcomes.canonicalLink(entity);
  return `<article class="outcome-card type-${esc(entity.entityType)} ${unknown || diagnostics.length ? "degraded" : ""}" data-outcome-type="${esc(entity.entityType)}" data-outcome-id="${esc(entity.entityId)}" tabindex="-1" style="--outcome-order:${Math.min(index, 12)}">
    <header class="outcome-card-head"><div class="outcome-type-mark" aria-hidden="true"><i></i><span>${icon}</span></div><div class="outcome-identity"><span>${esc(label)}</span><h3>${esc(entity.entityId)}</h3>${parent ? `<button type="button" data-outcome-parent="${esc(parent.entityId)}" title="Filter to canonical parent"><small>↳ ${esc(parent.entityType)} / ${esc(parent.entityId)}</small></button>` : `<small>root entity</small>`}</div>${unknown || diagnostics.length ? `<span class="outcome-degraded-badge">degraded</span>` : ""}</header>
    <div class="outcome-facets">${facets.map(([name, facet]) => {
      if (!facet) return `<div class="outcome-facet unknown"><span>${esc(name)}</span><b>missing</b><small>owner unavailable</small></div>`;
      const classes = [facet.known ? "known" : "unknown", facet.state === "not-applicable" ? "na" : "", outcomeFacetTerminal(facet) ? "terminal" : ""].filter(Boolean).join(" ");
      return `<div class="outcome-facet ${classes}"><span>${esc(name)}${outcomeFacetTerminal(facet) ? `<i title="Canonical raw state is terminal">◆</i>` : ""}</span><b>${esc(facet.state)}</b>${outcomeRaw(facet)}<small>${esc(facet.owner)}</small></div>`;
    }).join("")}</div>
    ${(diagnostics.length || link) ? `<footer class="outcome-card-foot">${diagnostics.length ? `<span><b>${diagnostics.length}</b> diagnostic${diagnostics.length === 1 ? "" : "s"}</span>` : `<span>canonical projection</span>`}${link ? `<button class="btn" type="button" data-outcome-tab="${esc(link.tab)}">${esc(link.label)} →</button>` : ""}</footer>` : ""}
  </article>`;
}

function renderOutcomeProjection() {
  const projection = state.outcomes.projection;
  const view = MissionOutcomes.projectionView(projection, state.outcomes.filters);
  const counts = view.counts;
  $("outcomeCount").textContent = counts.total;
  $("outcomeType").innerHTML = outcomeOption("all", state.outcomes.filters.type, `All types · ${counts.total}`)
    + view.options.types.map(type => outcomeOption(type, state.outcomes.filters.type, `${type} · ${counts.types[type]}`)).join("");
  $("outcomeState").innerHTML = outcomeOption("all", state.outcomes.filters.state, "All states")
    + view.options.states.map(value => outcomeOption(value, state.outcomes.filters.state)).join("");
  $("outcomeOwner").innerHTML = outcomeOption("all", state.outcomes.filters.owner, "All owners")
    + view.options.owners.map(value => outcomeOption(value, state.outcomes.filters.owner)).join("");
  $("outcomeSummary").innerHTML = `<div><b>${counts.total}</b><span>entities</span></div><div><b>${counts.shown}</b><span>shown</span></div><div><b>${counts.terminal}</b><span>terminal</span></div><div class="${counts.unknown ? "warn" : ""}"><b>${counts.unknown}</b><span>unknown</span></div><div class="${counts.degraded ? "warn" : ""}"><b>${counts.degraded}</b><span>degraded</span></div>`;
  $("outcomeAnnouncement").textContent = `Showing ${counts.shown} of ${counts.total} canonical outcomes.`;
  const diagnostics = Array.isArray(projection?.diagnostics) ? projection.diagnostics : [];
  let integrity = "";
  if (state.outcomes.integrity === "missing") integrity = `<div class="outcome-alert neutral"><b>Outcome projection unavailable</b><span>This older snapshot has no canonical outcome surface.</span></div>`;
  else if (state.outcomes.integrity === "legacy") integrity = `<div class="outcome-alert warn"><b>Legacy projection</b><span>Exact canonical outcome bytes are unavailable; local semantic equality may preserve UI state without claiming SHA-256 verification.</span></div>`;
  else if (state.outcomes.integrity === "mismatch") integrity = `<div class="outcome-alert bad"><b>Semantic integrity mismatch</b><span>The claimed outcome identity did not match the canonical client-side digest.</span></div>`;
  else if (state.outcomes.integrity === "invalid-envelope") integrity = `<div class="outcome-alert bad"><b>Semantic envelope rejected</b><span>The authoritative outcome bytes were malformed or used the wrong identity domain.</span></div>`;
  else if (state.outcomes.integrity === "unavailable") integrity = `<div class="outcome-alert warn"><b>Digest verification unavailable</b><span>The browser cannot verify SHA-256; canonical facets are shown with a bounded warning.</span></div>`;
  else if (projection?.degraded) integrity = `<div class="outcome-alert warn"><b>Canonical projection is degraded</b><span>${diagnostics.length} diagnostic${diagnostics.length === 1 ? "" : "s"}; unknown and dangling states are preserved without inference.</span></div>`;
  else integrity = `<div class="outcome-alert good"><b>Semantic projection verified</b><span>${esc(projection?.semanticSha256?.slice(0, 12) || "")}</span></div>`;
  $("outcomeIntegrity").innerHTML = integrity;
  const diagnosticHTML = diagnostics.length ? `<details class="outcome-diagnostics"><summary>${diagnostics.length} projection diagnostic${diagnostics.length === 1 ? "" : "s"}</summary><div>${diagnostics.map(item => `<article><b>${esc(item.code || "diagnostic")}</b><span>${esc(item.entityType || "projection")} / ${esc(item.entityId || "—")}${item.facet ? ` · ${esc(item.facet)}` : ""}</span><p>${esc(item.message || "Canonical projection reported a degraded state.")}</p></article>`).join("")}</div></details>` : "";
  $("outcomeResults").innerHTML = diagnosticHTML + (view.entities.length
    ? `<div class="outcome-grid">${view.entities.map(outcomeCard).join("")}</div>`
    : `<div class="empty compact"><b>${counts.total ? "No outcomes match these filters" : "No canonical outcomes yet"}</b>${counts.total ? "Change type, state, owner, terminal, or search filters." : "The workspace stays empty until the run snapshot projects canonical entities."}</div>`);
}

async function renderOutcomes(snapshot) {
  const renderGeneration = state.outcomes.renders.begin();
  $("outcomeResults").setAttribute("aria-busy", "true");
  try {
    const decision = await state.outcomes.tracker.accept(snapshot?.outcomeProjection);
    if (decision.action === "stale" || decision.action === "retain") return;
    state.outcomes.projection = decision.projection;
    state.outcomes.integrity = decision.integrity;
    renderOutcomeProjection();
  } finally {
    if (state.outcomes.renders.isCurrent(renderGeneration)) {
      $("outcomeResults").setAttribute("aria-busy", "false");
    }
  }
}

function renderDetail() {
  const snapshot = state.snapshot, run = snapshot.run, meta = snapshot.meta;
  const events = snapshot.events || [], artifacts = snapshot.artifacts || [], questions = snapshot.pendingQuestions || [];
  $("detailTitle").textContent = meta.target.split("/").pop(); $("detailGoal").textContent = meta.goal;
  const boundPolicy = meta.safetyPolicy, boundPolicyDocument = boundPolicy?.document;
  const boundPolicyLabel = boundPolicyDocument ? `${boundPolicyDocument.name} r${boundPolicyDocument.revision} · ${String(boundPolicy.policyId).slice(-10)}` : "legacy deny-all";
  const boundStrategy = meta.strategyMetadata, boundStrategyDocument = boundStrategy?.document;
  const boundStrategyLabel = boundStrategyDocument ? `${boundStrategyDocument.name} · ${String(boundStrategy.strategyId).slice(-10)}` : (meta.strategy || "legacy/custom");
  const boundStrategyRoles = boundStrategyDocument?.roles?.map(role => role.depends_on_roles?.length ? `${role.depends_on_roles.join("+")}→${role.role}` : role.role).join(" · ") || "not recorded";
  $("runMeta").innerHTML = [["status", run.status], ["run", run.id], ["model", meta.modelProfile.model], ["strategy", boundStrategyLabel], ["role graph", boundStrategyRoles], ["safety", boundPolicyLabel], ["coverage", `${snapshot.coverage.terminal}/${snapshot.coverage.workItemsTotal}`], ["target", meta.target]].map(([name, value]) => `<div class="kv"><span>${esc(name)}</span><span>${esc(value)}</span></div>`).join("");
  renderScope(meta);
  $("workers").innerHTML = snapshot.workers.map(worker => `<div class="worker"><div class="worker-top"><span class="worker-role">${esc(worker.role)}</span><span class="worker-status">${esc(worker.status)}</span></div><div class="worker-step">${esc(worker.current_step || "queued")}</div>${worker.error ? `<div class="worker-error">${esc(worker.error)}</div>` : ""}</div>`).join("");
  $("activitySummary").innerHTML = MissionObservability.activitySummary(events);
  $("events").innerHTML = events.map(MissionObservability.renderEvent).join("") || `<div class="empty compact"><b>No activity yet</b>Semantic model and worker events will stream here.</div>`;
  $("events").scrollTop = $("events").scrollHeight;
  $("reports").innerHTML = MissionObservability.renderReports(snapshot);
  $("usage").innerHTML = MissionObservability.renderUsage(snapshot);
  renderMemory(snapshot);
  renderKnowledge(snapshot);
  renderDossiers(snapshot);
  renderOutcomes(snapshot);
  $("artifacts").innerHTML = artifacts.map(artifact => `<div class="artifact" data-artifact-record="${esc(artifact.id)}" tabindex="-1"><b>${esc(artifact.kind)}</b><span>${esc(artifact.logical_path)}</span><a class="btn artifact-action" href="/api/runs/${encodeURIComponent(run.id)}/artifacts/${encodeURIComponent(artifact.id)}" download>Download</a></div>`).join("") || `<div class="empty compact"><b>No artifacts yet</b>Durable outputs will appear here.</div>`;
  renderEvidence();
  $("detailDecisions").innerHTML = questions.length ? questions.map(question => decisionHTML(run.id, question)).join("") : `<div class="empty compact"><b>No pending decisions</b>This run is not waiting on you.</div>`;
  $("activityCount").textContent = events.length;
  $("reportCount").textContent = MissionObservability.reportCount(snapshot);
  $("usageCount").textContent = (snapshot.modelCalls || []).length;
  $("artifactCount").textContent = artifacts.length;
  $("decisionCount").textContent = questions.length;
}

function connectEvents(runId) {
  if (state.stream) state.stream.close();
  state.snapshotRefreshes.invalidate();
  const stream = new EventSource(MissionOutcomes.eventStreamUrl(runId, state.streamCursors.get(runId)));
  state.stream = stream;
  const refresh = async event => {
    if (!MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) return;
    const refreshGeneration = state.snapshotRefreshes.begin();
    try {
      const [snapshot, reportPayload, evidence, dossierPayload] = await Promise.all([
        api(`/api/runs/${encodeURIComponent(runId)}`),
        api(`/api/runs/${encodeURIComponent(runId)}/reports`),
        api(`/api/runs/${encodeURIComponent(runId)}/evidence`),
        api(`/api/runs/${encodeURIComponent(runId)}/dossiers`),
      ]);
      if (!state.snapshotRefreshes.isCurrent(refreshGeneration)
          || !MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) return;
      snapshot.workerReports = reportPayload.reports;
      snapshot.dossiers = dossierPayload.dossiers;
      state.snapshot = snapshot;
      state.evidence = evidence.records || [];
      if (event.lastEventId) state.streamCursors.set(runId, event.lastEventId);
      renderDetail();
    } catch (error) {
      if (state.snapshotRefreshes.isCurrent(refreshGeneration)
          && MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) {
        toast(error.message, true);
      }
    } finally {
      if (MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) refreshFleet();
    }
  };
  stream.onmessage = refresh;
  stream.addEventListener("reset", refresh);
  stream.addEventListener("heartbeat", () => {
    if (MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) refreshFleet();
  });
  stream.onerror = () => {
    if (!MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) return;
    stream.close();
    setTimeout(() => {
      if (MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)) connectEvents(runId);
    }, 1500);
  };
}

function selectedProfile() {
  const profiles = state.config?.modelProfiles || [];
  return profiles.find(profile => profile.name === $("profileSelect").value) || state.config?.modelProfile;
}

function safetyPolicyByName(name) {
  return (state.config?.safetyPolicies || []).find(policy => policy.document?.name === name);
}

function selectedSafetyPolicy() {
  const name = document.querySelector('input[name="safetyPolicy"]:checked')?.value || "supervised";
  return safetyPolicyByName(name);
}

function selectedStrategyMetadata() {
  const name = $("strategySelect").value;
  return (state.config?.strategies || []).find(strategy => strategy.name === name);
}

function applyStrategyDefaults() {
  const ceilings = selectedStrategyMetadata()?.default_ceilings;
  if (!ceilings) return;
  const fields = {
    concurrency: "concurrency", retries_per_worker: "retriesPerWorker",
    max_workers: "maxWorkers", cost_units: "costUnits",
  };
  Object.entries(fields).forEach(([key, id]) => {
    if (Number.isInteger(ceilings[key])) $(id).value = ceilings[key];
  });
}

function positiveInteger(id, label, {allowZero = false} = {}) {
  const input = $(id), value = Number(input.value), minimum = allowZero ? 0 : 1;
  input.setCustomValidity(Number.isInteger(value) && value >= minimum ? "" : `${label} must be a whole number of at least ${minimum}.`);
  return value;
}

function renderComposerState() {
  const profile = selectedProfile(), strategy = $("strategySelect").value;
  const custom = strategy === "custom-roles";
  $("customRolesField").hidden = !custom;
  $("roles").disabled = !custom;
  const selected = selectedStrategyMetadata();
  $("strategyHint").textContent = selected?.description || "Define a comma-separated set of independent worker roles.";
  if (selected) {
    const roles = selected.roles || [], profileNames = selected.compatible_profile_names || [];
    const compatiblePolicies = selected.policy_constraints?.compatible_policy_ids || [];
    const roleGraph = roles.map(role => role.depends_on_roles?.length
      ? `${role.depends_on_roles.join(" + ")} → ${role.role}` : role.role).join(" · ");
    $("strategyPreview").innerHTML = `<div><span>durable identity</span><b>${esc(selected.strategyId?.slice(-14) || "unavailable")}</b><small>content-bound strategy</small></div><div><span>worker graph</span><b>${esc(roleGraph)}</b><small>${roles.length} seeded role${roles.length === 1 ? "" : "s"}</small></div><div><span>compatible profiles</span><b>${esc(profileNames.join(" · ") || "none")}</b><small>server enforced</small></div><div><span>compatible policies</span><b>${esc(compatiblePolicies.length)} exact identit${compatiblePolicies.length === 1 ? "y" : "ies"}</b><small>server enforced</small></div>`;
  } else $("strategyPreview").innerHTML = "";
  if (profile) {
    $("effectivePolicy").innerHTML = `<div class="policy-item"><span>structured output</span><b>${esc(profile.structuredOutputMode || "prompted")}</b></div><div class="policy-item"><span>provider concurrency</span><b>${esc(profile.concurrencyLimit ?? "not declared")}</b></div><div class="policy-item"><span>provider retries</span><b>${esc(profile.retryLimit ?? "not declared")}</b></div>`;
    $("modelStack").textContent = profile.model;
    if (Number.isInteger(profile.concurrencyLimit)) {
      $("concurrency").max = profile.concurrencyLimit;
      if (Number($("concurrency").value) > profile.concurrencyLimit) $("concurrency").value = profile.concurrencyLimit;
    } else $("concurrency").removeAttribute("max");
  }
  const policy = document.querySelector('input[name="safetyPolicy"]:checked')?.value || "supervised";
  const policyRecord = safetyPolicyByName(policy);
  document.querySelectorAll(".safety-option").forEach(option => option.classList.toggle("active", option.querySelector("input").checked));
  $("gatedPolicyLabel").textContent = policy === "automatic-only" ? "excluded" : "ask me";
  $("toolPicker").querySelectorAll(".tool").forEach(row => {
    const input = row.querySelector("input"), excluded = policy === "automatic-only" && input.dataset.gated === "true";
    if (excluded) input.checked = false;
    input.disabled = excluded;
    row.classList.toggle("policy-disabled", excluded);
  });
  const checkedTools = $("toolPicker").querySelectorAll("input:checked").length;
  const policyIdentity = policyRecord?.policyId ? policyRecord.policyId.slice(-10) : "unavailable";
  $("launchSummary").innerHTML = `<div><span>strategy</span><b>${esc(custom ? "custom roles" : strategy)}</b></div><div><span>profile</span><b>${esc(profile?.name || "default")}</b></div><div><span>worker ceiling</span><b>${esc($("maxWorkers").value)} / ${esc($("concurrency").value)} concurrent</b></div><div><span>tool policy</span><b>${esc(policy)} r${esc(policyRecord?.document?.revision || "—")} · ${esc(policyIdentity)} · ${checkedTools} selected</b></div>`;
}

function renderConfig() {
  const config = state.config, active = config.modelProfile, profiles = config.modelProfiles || [active];
  $("modelStack").textContent = active.model;
  $("profileSelect").innerHTML = profiles.map(profile => `<option value="${esc(profile.name)}" ${profile.name === config.defaultModelProfile ? "selected" : ""}>${esc(profile.name)} · ${esc(profile.model)} · ${esc(profile.apiFormat || "openai")}</option>`).join("");
  const strategies = config.strategies || [];
  $("strategySelect").innerHTML = strategies.map(strategy => `<option value="${esc(strategy.name)}">${esc(strategy.name)}</option>`).join("") + `<option value="custom-roles">custom roles</option>`;
  document.querySelectorAll('[data-policy-name]').forEach(option => {
    const available = Boolean(safetyPolicyByName(option.dataset.policyName));
    option.querySelector("input").disabled = !available;
    option.classList.toggle("policy-disabled", !available);
  });
  $("profile").innerHTML = profiles.map(profile => `<div class="profile-block"><div class="kv"><span>profile</span><span>${esc(profile.name)}</span></div><div class="kv"><span>provider</span><span>${esc(profile.provider)}</span></div><div class="kv"><span>protocol</span><span>${esc(profile.apiFormat || "openai")}</span></div><div class="kv"><span>model</span><span class="model-name">${esc(profile.model)}</span></div><div class="kv"><span>endpoint</span><span>${esc(profile.baseUrl)}</span></div><div class="kv"><span>secret</span><span>${esc(profile.apiKeySource)}</span></div></div>`).join("");
  const knowledgeRoots = config.knowledgeRoots || [];
  $("knowledgeRootCatalog").innerHTML = knowledgeRoots.length ? knowledgeRoots.map(root => `<div class="knowledge-root-card"><i aria-hidden="true"></i><span><b>${esc(root.name)}</b><small>named OKF index · path withheld</small></span><em>read only</em></div>`).join("") : `<div class="hint">No knowledge roots configured.</div>`;
  $("settings").innerHTML = `<div class="kv"><span>storage root</span><span>${esc(config.storageRoot)}</span></div><div class="kv"><span>API boundary</span><span>loopback only</span></div><div class="kv"><span>event transport</span><span>SSE · resumable ID</span></div><div class="kv"><span>model profiles</span><span>${profiles.length}</span></div><div class="kv"><span>knowledge roots</span><span>${knowledgeRoots.length} named · paths withheld</span></div>`;
  setRestartState(false);
  const tools = config.tools || [];
  const toolRow = tool => `<label class="tool"><input type="checkbox" value="${esc(tool.id)}" data-gated="${Boolean(tool.requires_permission)}"><span class="tool-name">${esc(tool.id)}<small>${esc(tool.source || "default")}</small></span><span class="tier ${tool.requires_permission ? "gated" : ""}">${tool.requires_permission ? "ask" : "auto"} · t${tool.safety_tier}</span></label>`;
  $("toolPicker").innerHTML = tools.map(toolRow).join("") || `<div class="hint">No Rekit tools discovered.</div>`;
  $("toolCatalog").innerHTML = tools.map(tool => `<div class="tool"><span class="tool-name">${esc(tool.id)}<small>${esc(tool.source || "default")}</small></span><span class="tier ${tool.requires_permission ? "gated" : ""}">${tool.requires_permission ? "permission" : "automatic"} · tier ${tool.safety_tier}</span></div>`).join("");
  $("profileSelect").addEventListener("change", renderComposerState);
  $("strategySelect").addEventListener("change", () => { applyStrategyDefaults(); renderComposerState(); });
  document.querySelectorAll('input[name="safetyPolicy"]').forEach(input => input.addEventListener("change", renderComposerState));
  ["concurrency", "retriesPerWorker", "maxWorkers", "costUnits", "roles"].forEach(id => $(id).addEventListener("input", renderComposerState));
  $("toolPicker").addEventListener("change", renderComposerState);
  applyStrategyDefaults();
  renderComposerState();
}

function renderNotifications() {
  const list = $("notificationList");
  $("notificationRunLabel").textContent = state.notificationRun ? `run · ${state.notificationRun}` : "no run selected";
  if (!state.notificationRun) {
    list.innerHTML = `<div class="empty compact"><b>No investigations yet</b>Durable delivery state will appear after a run emits a consequential transition.</div>`;
    return;
  }
  list.innerHTML = state.notifications.length ? state.notifications.map(notification => {
    const payload = notification.payload, link = payload.deepLink;
    const route = MissionNotifications.exactRoute(link);
    const canAcknowledge = notification.status === "sent";
    return `<article class="notification-row notification-${esc(notification.status)}"><div class="notification-signal" aria-hidden="true"></div><div><span>${esc(payload.kind.replaceAll(".", " / "))}</span><b>${esc(payload.message)}</b><small>${esc(notification.status)} · ${esc(new Date(notification.updatedAt).toLocaleString())}</small>${notification.lastErrorCode ? `<code>${esc(notification.lastErrorCode)}</code>` : ""}</div><div class="notification-actions"><button class="btn" type="button" data-notification-preview data-notification-id="${esc(notification.id)}">Preview</button>${route ? `<button class="btn" type="button" data-notification-link${route.runId ? ` data-run="${esc(route.runId)}"` : ""} data-surface="${esc(route.surface)}" data-entity-type="${esc(route.entityType)}" data-entity-id="${esc(route.entityId)}">Open ${esc(route.entityType)}</button>` : `<button class="btn" type="button" disabled title="Exact notification route is unavailable">Route unavailable</button>`}${canAcknowledge ? `<button class="btn primary" type="button" data-notification-ack data-notification-id="${esc(notification.id)}" data-notification-revision="${esc(notification.revision)}">Acknowledge</button>` : ""}</div></article>`;
  }).join("") : `<div class="empty compact"><b>No delivery records</b>This run has no consequential notifications in its durable outbox.</div>`;
}

function renderNotificationConfiguration() {
  const configuration = state.notificationConfiguration, root = $("notificationConfiguration");
  if (!configuration) { root.innerHTML = `<div class="empty compact"><b>Delivery policy unavailable</b></div>`; return; }
  root.innerHTML = `<div class="notification-field"><label><span>Delivery policy</span><select id="notificationPreferencePreset">${configuration.preferencePresets.map(preset => `<option value="${esc(preset.id)}" ${preset.id === configuration.preferencePresetId ? "selected" : ""}>${esc(preset.id)} · ${esc(preset.mode)}${preset.parameter === null ? "" : ` · ${esc(preset.parameter)}`}</option>`).join("")}</select></label><label><span>Notify finding at</span><select id="notificationFindingStage">${configuration.findingNotificationStages.map(stage => `<option value="${esc(stage.id)}" ${stage.id === configuration.findingNotificationStageId ? "selected" : ""}>${esc(stage.id)}</option>`).join("")}</select></label><small>One server-declared threshold per finding. Policy identity is revision-bound.</small></div><div class="notification-field"><span>Enabled channels</span><div class="notification-channel-list">${configuration.channels.map(channel => `<div class="notification-channel"><input id="notification-channel-${esc(channel.ref)}" type="checkbox" data-notification-channel-ref value="${esc(channel.ref)}" ${configuration.channelRefs.includes(channel.ref) ? "checked" : ""}><label for="notification-channel-${esc(channel.ref)}"><b>${esc(channel.ref)}</b><small>${esc(channel.kind)} · endpoint and credentials withheld</small></label><button class="btn" type="button" data-notification-channel-test data-channel-ref="${esc(channel.ref)}">Test</button></div>`).join("")}</div></div>`;
}

function renderNotificationPreview(preview, channelRef = null) {
  const root = $("notificationPreview"); root.hidden = false;
  root.innerHTML = `<header><b>${esc(preview.title)}</b><span>${esc(channelRef || preview.kind)}</span></header><p>${esc(preview.message)}</p><code>${esc(preview.idempotencyKey)}</code>`;
}

async function loadNotificationConfiguration() {
  try {
    const result = await api("/api/notification-configuration");
    state.notificationConfiguration = result.configuration; renderNotificationConfiguration();
  } catch (error) { toast(error.message, true); }
}

async function saveNotificationConfiguration() {
  const configuration = state.notificationConfiguration;
  const channelRefs = [...document.querySelectorAll("[data-notification-channel-ref]:checked")].map(input => input.value);
  try {
    const result = await api("/api/notification-configuration", {method: "POST", body: JSON.stringify({expectedRevision: configuration.revision, preferencePresetId: $("notificationPreferencePreset").value, channelRefs, findingNotificationStageId: $("notificationFindingStage").value})});
    state.notificationConfiguration = result.configuration; renderNotificationConfiguration(); toast("Delivery policy saved");
  } catch (error) { await loadNotificationConfiguration(); toast(error.message, true); }
}

async function testNotificationChannel(button) {
  button.disabled = true;
  try {
    const testId = `ui-${crypto.randomUUID()}`;
    const result = await api(`/api/notification-configuration/channels/${encodeURIComponent(button.dataset.channelRef)}/test`, {method: "POST", body: JSON.stringify({expectedRevision: state.notificationConfiguration.revision, testId})});
    renderNotificationPreview(result.preview, result.channelRef); toast(result.sent ? "Fixed channel test sent" : `Channel test: ${result.errorCode}`, !result.sent);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function previewNotification(button) {
  try {
    const result = await api(`/api/runs/${encodeURIComponent(state.notificationRun)}/notifications/${encodeURIComponent(button.dataset.notificationId)}/preview`);
    renderNotificationPreview(result.preview);
  } catch (error) { toast(error.message, true); }
}

async function loadNotifications() {
  const runId = state.selected || state.fleet[0]?.runId || state.fleet[0]?.id || null;
  state.notificationRun = runId;
  if (!runId) { state.notifications = []; renderNotifications(); return; }
  try {
    const result = await api(`/api/runs/${encodeURIComponent(runId)}/notifications`);
    if (state.notificationRun !== runId) return;
    state.notifications = result.notifications || [];
    renderNotifications();
  } catch (error) { toast(error.message, true); }
}

async function acknowledgeNotification(button) {
  button.disabled = true;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(state.notificationRun)}/notifications/${encodeURIComponent(button.dataset.notificationId)}/acknowledge`, {method: "POST", body: JSON.stringify({expectedRevision: button.dataset.notificationRevision})});
    state.notifications = state.notifications.map(item => item.id === result.notification.id ? result.notification : item);
    renderNotifications(); toast("Notification acknowledged");
  } catch (error) { button.disabled = false; toast(error.message, true); }
}

async function openNotificationLink(button) {
  const candidate = {view: "mission-control",
    tab: ({campaigns: "campaigns", decisions: "decisions", outcomes: "findings", dossiers: "dossiers"})[button.dataset.surface],
    entityType: button.dataset.entityType, entityId: button.dataset.entityId};
  if (button.dataset.run) candidate.runId = button.dataset.run;
  const route = MissionNotifications.exactRoute(candidate);
  if (!route) { toast("The notification route is invalid and was not opened.", true); return; }
  await navigateExactRoute(route, {persist: true});
}

function persistExactRoute(route) {
  const search = MissionNotifications.urlSearch(route, state.config?.navigationRoute);
  if (!search) return false;
  window.history.replaceState(null, "", `${window.location.pathname}${search}`);
  return true;
}

function clearExactRoute() {
  if (window.location.search) window.history.replaceState(
    null, "", `${window.location.pathname}${window.location.hash}`,
  );
}

async function navigateExactRoute(route, {persist = false} = {}) {
  if (route.surface === "campaigns") {
    const plan = MissionNotifications.focusPlan(route, state.snapshot, state.campaigns);
    if (!plan) return false;
    show("campaigns"); await openCampaign(route.entityId);
    if (state.campaignSelected !== route.entityId) return false;
    if (persist) persistExactRoute(route);
    return true;
  }
  await openRun(route.runId);
  if (state.selected !== route.runId || state.snapshot?.run?.id !== route.runId) return false;
  const plan = MissionNotifications.focusPlan(route, state.snapshot, state.campaigns);
  if (!plan) return false;
  if (plan.surface === "outcomes") {
    await renderOutcomes(state.snapshot);
    state.outcomes.filters = plan.outcomeFilters;
    $("outcomeSearch").value = plan.entityId; renderOutcomeProjection();
  }
  activateDetailTab($(`tab-button-${plan.surface}`), {focus: true});
  const exact = plan.surface === "decisions"
    ? document.querySelector(`[data-decision-id="${CSS.escape(plan.entityId)}"]`)
    : plan.surface === "dossiers"
      ? document.querySelector(`[data-dossier-id="${CSS.escape(plan.entityId)}"]`)
      : document.querySelector(`[data-outcome-id="${CSS.escape(plan.entityId)}"][data-outcome-type="${CSS.escape(plan.entityType)}"]`);
  if (!exact) return false;
  exact.classList.add("notification-linked-target");
  exact.scrollIntoView({block: "center"}); exact.focus({preventScroll: true});
  if (persist) persistExactRoute(route);
  return true;
}

async function restoreExactRoute() {
  if (!window.location.search) return;
  const route = MissionNotifications.parseUrlRoute(
    window.location.search, state.config?.navigationRoute,
  );
  if (!route || !await navigateExactRoute(route)) {
    clearExactRoute();
    toast("The saved Mission Control route is stale or invalid and was not restored.", true);
  }
}

$("runForm").onsubmit = async event => {
  event.preventDefault(); const button = $("launchBtn"); button.disabled = true; button.textContent = "Launching…";
  try {
    const modelTools = [...$("toolPicker").querySelectorAll("input:checked")].map(input => input.value);
    const workerRoles = $("roles").value.split(",").map(role => role.trim()).filter(Boolean);
    const strategy = $("strategySelect").value;
    const concurrency = positiveInteger("concurrency", "Concurrent workers");
    const retriesPerWorker = positiveInteger("retriesPerWorker", "Retries per worker", {allowZero: true});
    const maxWorkers = positiveInteger("maxWorkers", "Maximum workers");
    const costUnits = positiveInteger("costUnits", "Cost-unit ceiling");
    if (concurrency > maxWorkers) $("concurrency").setCustomValidity("Concurrent workers cannot exceed the maximum worker ceiling.");
    const profile = selectedProfile();
    const safetyPolicy = selectedSafetyPolicy();
    if (!safetyPolicy?.policyId) throw new Error("Selected safety policy is unavailable or stale; reload Mission Control.");
    if (Number.isInteger(profile?.concurrencyLimit) && concurrency > profile.concurrencyLimit) $("concurrency").setCustomValidity(`This profile allows at most ${profile.concurrencyLimit} concurrent workers.`);
    if (strategy === "custom-roles" && !workerRoles.length) $("roles").setCustomValidity("Add at least one custom worker role."); else $("roles").setCustomValidity("");
    if (!$("runForm").reportValidity()) return;
    const result = await api("/api/runs", {method: "POST", body: JSON.stringify({target: $("target").value, goal: $("goal").value, tools: [], modelTools, workerRoles, modelProfile: $("profileSelect").value, safetyPolicyId: safetyPolicy.policyId, strategy: strategy === "custom-roles" ? null : strategy, concurrency, retriesPerWorker, costUnits, maxWorkers})});
    toast("Investigation launched"); await refreshFleet(); openRun(result.run.id);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Launch Investigation"; }
};

async function boot() {
  document.querySelectorAll('[role="tab"]').forEach(tab => {
    tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1;
  });
  try {
    state.config = await api("/api/config"); renderConfig();
    $("campaignDialog").addEventListener("cancel", event => { event.preventDefault(); closeCampaign(); });
    $("fleetSearch").addEventListener("input", event => { state.query = event.target.value; renderFleet(); });
    await Promise.all([refreshFleet(), refreshCampaigns()]);
    await restoreExactRoute();
    setInterval(() => { refreshFleet(); refreshCampaigns(); }, 1800);
  }
  catch (error) { toast(error.message, true); }
}

initializeTheme();
boot();
