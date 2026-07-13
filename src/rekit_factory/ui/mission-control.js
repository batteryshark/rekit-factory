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
    return (snapshot.workItems || []).flatMap(item => {
      const result = item.result;
      if (!result || typeof result !== "object" || !first(result, "summary", "observations", "next_actions", "nextActions")) return [];
      return [{role: first(item.payload, "role", "workerRole") || item.category || "worker", title: item.title || "Worker report", summary: result.summary || "Report completed.", observations: result.observations || [], next: first(result, "next_actions", "nextActions") || [], status: first(result, "status_update", "statusUpdate") || item.state_label || item.status}];
    });
  }
  function renderReports(snapshot) {
    const items = reports(snapshot);
    if (!items.length) return `<div class="empty compact"><b>No worker reports yet</b>Completed structured worker results will collect here.</div>`;
    return items.map(report => `<article class="report-card"><header><div><span class="report-role">${safe(report.role)}</span><h3>${safe(report.title)}</h3></div><span class="report-status">${safe(report.status)}</span></header><p>${safe(report.summary)}</p>${report.observations.length ? `<div class="report-section"><b>Observations</b><ul>${report.observations.map(item => `<li>${safe(item)}</li>`).join("")}</ul></div>` : ""}${report.next.length ? `<div class="report-section next"><b>Next actions</b><ul>${report.next.map(item => `<li>${safe(item)}</li>`).join("")}</ul></div>` : ""}</article>`).join("");
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
    return `<article class="decision decision-${variant}"><div class="decision-head"><div class="decision-icon" aria-hidden="true">${variant === "permission" ? "!" : variant === "missing-tool" ? "?" : variant === "direction" ? "↗" : "◇"}</div><div><b>${safe(String(kind).replaceAll("_", " "))}</b><span>${safe(runId)}</span></div></div><div class="decision-body"><div class="question">${safe(prompt)}</div>${context.length ? `<div class="decision-context">${context.map(([label, value]) => `<span><b>${safe(label)}</b>${safe(value)}</span>`).join("")}</div>` : ""}<div class="choices">${options.length ? options.map((option, index) => `<button class="btn ${String(option.value).toLowerCase() === "allow" ? "primary" : index === 0 && variant === "direction" ? "primary" : String(option.value).toLowerCase() === "deny" ? "red" : ""}" data-answer="${safe(option.value)}" data-run="${safe(runId)}" data-question="${safe(question.id)}">${safe(option.label)}</button>`).join("") : `<span class="decision-unavailable">No response options supplied by this question.</span>`}</div></div></article>`;
  }
  return {activitySummary, renderDecision, renderEvent, renderReports, renderUsage, reportCount: snapshot => reports(snapshot).length};
})();

const state = {fleet: [], config: null, filter: "all", selected: null, snapshot: null, stream: null};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
})[character]);

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

function show(name) {
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav").forEach(nav => nav.classList.toggle("active", nav.dataset.view === name));
  $("crumb").textContent = name.replaceAll("-", " ").toUpperCase();
  if (name === "inbox") loadInbox();
}

function activate(element) {
  const view = element.closest("[data-view]");
  if (view) { show(view.dataset.view); return true; }
  const card = element.closest("[data-run]");
  if (card) { openRun(card.dataset.run); return true; }
  const answer = element.closest("[data-answer]");
  if (answer) { resolveDecision(answer.dataset.run, answer.dataset.question, answer.dataset.answer); return true; }
  const tab = element.closest("[data-tab]");
  if (tab) {
    document.querySelectorAll(".tab").forEach(item => {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tabpane").forEach(item => item.classList.toggle("active", item.id === `tab-${tab.dataset.tab}`));
    return true;
  }
  return false;
}

document.addEventListener("click", event => activate(event.target));
document.addEventListener("keydown", event => {
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-run]")) {
    event.preventDefault();
    activate(event.target);
  }
});

function statusRank(status) {
  return ({needs_input: 0, running: 1, queued: 2, failed: 3, completed: 4, blocked: 5})[status] ?? 9;
}

function latestStep(run) {
  const active = run.workers.find(worker => worker.status === "running") || run.workers.find(worker => worker.current_step);
  return active?.current_step || run.latestEvent?.message || "awaiting work";
}

function renderFleet() {
  const runs = [...state.fleet].sort((left, right) => statusRank(left.status) - statusRank(right.status));
  const counts = {all: runs.length};
  runs.forEach(run => { counts[run.status] = (counts[run.status] || 0) + 1; });
  $("filters").innerHTML = ["all", "needs_input", "running", "queued", "failed", "completed"].map(key =>
    `<button class="chip ${state.filter === key ? "active" : ""}" data-filter="${key}">${key.replace("_", " ")} · ${counts[key] || 0}</button>`
  ).join("");
  document.querySelectorAll("[data-filter]").forEach(button => { button.onclick = () => { state.filter = button.dataset.filter; renderFleet(); }; });
  const shown = runs.filter(run => state.filter === "all" || run.status === state.filter);
  $("fleet").innerHTML = shown.length ? shown.map(run => `
    <article class="card ${esc(run.status)}" data-run="${esc(run.runId)}" tabindex="0" role="button" aria-label="Open ${esc(run.target.split("/").pop())}">
      <div class="card-top"><div class="target">${esc(run.target.split("/").pop())}</div><div class="pill ${esc(run.status)}">${esc(run.status.replace("_", " "))}</div></div>
      <div class="goal">${esc(run.goal)}</div><div class="step">${esc(latestStep(run))}</div>
      <div class="card-meta"><span class="tag"><strong>${esc(run.modelProfile.model)}</strong></span><span class="tag">${run.workers.length} workers</span>${run.needsYou ? `<span class="tag warn">${run.needsYou} needs you</span>` : ""}</div>
      <div class="counts"><div class="count"><b>${run.coverage.done}</b><span>done</span></div><div class="count"><b>${run.coverage.pending}</b><span>pending</span></div><div class="count"><b>${run.coverage.failed}</b><span>failed</span></div></div>
    </article>`).join("") : `<div class="empty"><b>No investigations here</b>Launch a run or change the filter.</div>`;
  const running = runs.filter(run => run.status === "running").length;
  const needs = runs.reduce((total, run) => total + run.needsYou, 0);
  $("fleetSub").textContent = `${runs.length} investigations · ${running} running · ${needs} awaiting your call`;
  $("stats").innerHTML = `<span class="stat"><b>${runs.length}</b> runs</span><span class="stat"><b>${runs.reduce((total, run) => total + run.workers.length, 0)}</b> workers</span>`;
  $("healthText").textContent = `${running} active · local`;
  $("inboxBadge").textContent = needs;
  $("inboxBadge").style.display = needs ? "grid" : "none";
}

async function refreshFleet() {
  try { state.fleet = (await api("/api/fleet")).runs; renderFleet(); }
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
  try { state.selected = runId; state.snapshot = await api(`/api/runs/${encodeURIComponent(runId)}`); renderDetail(); show("detail"); connectEvents(runId); }
  catch (error) { toast(error.message, true); }
}

function renderDetail() {
  const snapshot = state.snapshot, run = snapshot.run, meta = snapshot.meta;
  const events = snapshot.events || [], artifacts = snapshot.artifacts || [], questions = snapshot.pendingQuestions || [];
  $("detailTitle").textContent = meta.target.split("/").pop(); $("detailGoal").textContent = meta.goal;
  $("runMeta").innerHTML = [["status", run.status], ["run", run.id], ["model", meta.modelProfile.model], ["coverage", `${snapshot.coverage.terminal}/${snapshot.coverage.workItemsTotal}`], ["target", meta.target]].map(([name, value]) => `<div class="kv"><span>${esc(name)}</span><span>${esc(value)}</span></div>`).join("");
  $("workers").innerHTML = snapshot.workers.map(worker => `<div class="worker"><div class="worker-top"><span class="worker-role">${esc(worker.role)}</span><span class="worker-status">${esc(worker.status)}</span></div><div class="worker-step">${esc(worker.current_step || "queued")}</div>${worker.error ? `<div class="worker-error">${esc(worker.error)}</div>` : ""}</div>`).join("");
  $("activitySummary").innerHTML = MissionObservability.activitySummary(events);
  $("events").innerHTML = events.map(MissionObservability.renderEvent).join("") || `<div class="empty compact"><b>No activity yet</b>Semantic model and worker events will stream here.</div>`;
  $("events").scrollTop = $("events").scrollHeight;
  $("reports").innerHTML = MissionObservability.renderReports(snapshot);
  $("usage").innerHTML = MissionObservability.renderUsage(snapshot);
  $("artifacts").innerHTML = artifacts.map(artifact => `<div class="artifact"><b>${esc(artifact.kind)}</b><span>${esc(artifact.logical_path)}</span></div>`).join("") || `<div class="empty compact"><b>No artifacts yet</b>Durable outputs will appear here.</div>`;
  $("detailDecisions").innerHTML = questions.length ? questions.map(question => decisionHTML(run.id, question)).join("") : `<div class="empty compact"><b>No pending decisions</b>This run is not waiting on you.</div>`;
  $("activityCount").textContent = events.length;
  $("reportCount").textContent = MissionObservability.reportCount(snapshot);
  $("usageCount").textContent = (snapshot.modelCalls || []).length;
  $("artifactCount").textContent = artifacts.length;
  $("decisionCount").textContent = questions.length;
}

function connectEvents(runId) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  state.stream.onmessage = async () => { if (state.selected === runId) { state.snapshot = await api(`/api/runs/${encodeURIComponent(runId)}`); renderDetail(); } refreshFleet(); };
  state.stream.addEventListener("heartbeat", () => refreshFleet());
  state.stream.onerror = () => { state.stream.close(); setTimeout(() => state.selected === runId && connectEvents(runId), 1500); };
}

function selectedProfile() {
  const profiles = state.config?.modelProfiles || [];
  return profiles.find(profile => profile.name === $("profileSelect").value) || state.config?.modelProfile;
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
  const selected = (state.config.strategies || []).find(item => item.name === strategy);
  $("strategyHint").textContent = selected?.description || "Define a comma-separated set of independent worker roles.";
  if (profile) {
    $("effectivePolicy").innerHTML = `<div class="policy-item"><span>structured output</span><b>${esc(profile.structuredOutputMode || "prompted")}</b></div><div class="policy-item"><span>provider concurrency</span><b>${esc(profile.concurrencyLimit ?? "not declared")}</b></div><div class="policy-item"><span>provider retries</span><b>${esc(profile.retryLimit ?? "not declared")}</b></div>`;
    $("modelStack").textContent = profile.model;
    if (Number.isInteger(profile.concurrencyLimit)) {
      $("concurrency").max = profile.concurrencyLimit;
      if (Number($("concurrency").value) > profile.concurrencyLimit) $("concurrency").value = profile.concurrencyLimit;
    } else $("concurrency").removeAttribute("max");
  }
  const policy = document.querySelector('input[name="safetyPolicy"]:checked')?.value || "supervised";
  document.querySelectorAll(".safety-option").forEach(option => option.classList.toggle("active", option.querySelector("input").checked));
  $("gatedPolicyLabel").textContent = policy === "automatic-only" ? "excluded" : "ask me";
  $("toolPicker").querySelectorAll(".tool").forEach(row => {
    const input = row.querySelector("input"), excluded = policy === "automatic-only" && input.dataset.gated === "true";
    if (excluded) input.checked = false;
    input.disabled = excluded;
    row.classList.toggle("policy-disabled", excluded);
  });
  const checkedTools = $("toolPicker").querySelectorAll("input:checked").length;
  $("launchSummary").innerHTML = `<div><span>strategy</span><b>${esc(custom ? "custom roles" : strategy)}</b></div><div><span>profile</span><b>${esc(profile?.name || "default")}</b></div><div><span>worker ceiling</span><b>${esc($("maxWorkers").value)} / ${esc($("concurrency").value)} concurrent</b></div><div><span>tool policy</span><b>${esc(policy)} · ${checkedTools} selected</b></div>`;
}

function renderConfig() {
  const config = state.config, active = config.modelProfile, profiles = config.modelProfiles || [active];
  $("modelStack").textContent = active.model;
  $("profileSelect").innerHTML = profiles.map(profile => `<option value="${esc(profile.name)}" ${profile.name === config.defaultModelProfile ? "selected" : ""}>${esc(profile.name)} · ${esc(profile.model)} · ${esc(profile.apiFormat || "openai")}</option>`).join("");
  const strategies = config.strategies || [];
  $("strategySelect").innerHTML = strategies.map(strategy => `<option value="${esc(strategy.name)}">${esc(strategy.name)}</option>`).join("") + `<option value="custom-roles">custom roles</option>`;
  $("profile").innerHTML = profiles.map(profile => `<div class="profile-block"><div class="kv"><span>profile</span><span>${esc(profile.name)}</span></div><div class="kv"><span>provider</span><span>${esc(profile.provider)}</span></div><div class="kv"><span>protocol</span><span>${esc(profile.apiFormat || "openai")}</span></div><div class="kv"><span>model</span><span class="model-name">${esc(profile.model)}</span></div><div class="kv"><span>endpoint</span><span>${esc(profile.baseUrl)}</span></div><div class="kv"><span>secret</span><span>${esc(profile.apiKeySource)}</span></div></div>`).join("");
  $("settings").innerHTML = `<div class="kv"><span>storage root</span><span>${esc(config.storageRoot)}</span></div><div class="kv"><span>API boundary</span><span>loopback only</span></div><div class="kv"><span>event transport</span><span>SSE · resumable ID</span></div><div class="kv"><span>model profiles</span><span>${profiles.length}</span></div>`;
  const tools = config.tools || [];
  const toolRow = tool => `<label class="tool"><input type="checkbox" value="${esc(tool.id)}" data-gated="${Boolean(tool.requires_permission)}"><span class="tool-name">${esc(tool.id)}<small>${esc(tool.source || "default")}</small></span><span class="tier ${tool.requires_permission ? "gated" : ""}">${tool.requires_permission ? "ask" : "auto"} · t${tool.safety_tier}</span></label>`;
  $("toolPicker").innerHTML = tools.map(toolRow).join("") || `<div class="hint">No Rekit tools discovered.</div>`;
  $("toolCatalog").innerHTML = tools.map(tool => `<div class="tool"><span class="tool-name">${esc(tool.id)}<small>${esc(tool.source || "default")}</small></span><span class="tier ${tool.requires_permission ? "gated" : ""}">${tool.requires_permission ? "permission" : "automatic"} · tier ${tool.safety_tier}</span></div>`).join("");
  $("profileSelect").addEventListener("change", renderComposerState);
  $("strategySelect").addEventListener("change", renderComposerState);
  document.querySelectorAll('input[name="safetyPolicy"]').forEach(input => input.addEventListener("change", renderComposerState));
  ["concurrency", "retriesPerWorker", "maxWorkers", "costUnits", "roles"].forEach(id => $(id).addEventListener("input", renderComposerState));
  $("toolPicker").addEventListener("change", renderComposerState);
  renderComposerState();
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
    if (Number.isInteger(profile?.concurrencyLimit) && concurrency > profile.concurrencyLimit) $("concurrency").setCustomValidity(`This profile allows at most ${profile.concurrencyLimit} concurrent workers.`);
    if (strategy === "custom-roles" && !workerRoles.length) $("roles").setCustomValidity("Add at least one custom worker role."); else $("roles").setCustomValidity("");
    if (!$("runForm").reportValidity()) return;
    const result = await api("/api/runs", {method: "POST", body: JSON.stringify({target: $("target").value, goal: $("goal").value, tools: [], modelTools, workerRoles, modelProfile: $("profileSelect").value, strategy: strategy === "custom-roles" ? null : strategy, concurrency, retriesPerWorker, costUnits, maxWorkers})});
    toast("Investigation launched"); await refreshFleet(); openRun(result.run.id);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Launch Investigation"; }
};

async function boot() {
  try { state.config = await api("/api/config"); renderConfig(); await refreshFleet(); setInterval(refreshFleet, 2500); }
  catch (error) { toast(error.message, true); }
}

boot();
