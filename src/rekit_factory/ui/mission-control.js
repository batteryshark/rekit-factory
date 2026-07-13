"use strict";

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
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
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
  return `<article class="decision"><div class="decision-head"><b>${esc(question.kind)}</b><span>${esc(runId)}</span></div><div class="decision-body"><div class="question">${esc(question.prompt)}</div><div class="choices">${question.options.map(option => `<button class="btn ${option === "allow" ? "primary" : "red"}" data-answer="${esc(option)}" data-run="${esc(runId)}" data-question="${esc(question.id)}">${esc(option)}</button>`).join("")}</div></div></article>`;
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
  $("detailTitle").textContent = meta.target.split("/").pop(); $("detailGoal").textContent = meta.goal;
  $("runMeta").innerHTML = [["status", run.status], ["run", run.id], ["model", meta.modelProfile.model], ["coverage", `${snapshot.coverage.terminal}/${snapshot.coverage.workItemsTotal}`], ["target", meta.target]].map(([name, value]) => `<div class="kv"><span>${esc(name)}</span><span>${esc(value)}</span></div>`).join("");
  $("workers").innerHTML = snapshot.workers.map(worker => `<div class="worker"><div class="worker-top"><span class="worker-role">${esc(worker.role)}</span><span class="worker-status">${esc(worker.status)}</span></div><div class="worker-step">${esc(worker.current_step || "queued")}</div>${worker.error ? `<div class="worker-error">${esc(worker.error)}</div>` : ""}</div>`).join("");
  $("events").innerHTML = snapshot.events.map(event => `<div class="event"><time>${esc(new Date(event.created_at).toLocaleTimeString())}</time><span class="kind">${esc(event.kind)}</span><span class="msg">${esc(event.message)}</span></div>`).join("") || `<div class="empty">No events yet.</div>`;
  $("events").scrollTop = $("events").scrollHeight;
  $("artifacts").innerHTML = snapshot.artifacts.map(artifact => `<div class="artifact"><b>${esc(artifact.kind)}</b><span>${esc(artifact.logical_path)}</span></div>`).join("") || `<div class="empty">No artifacts yet.</div>`;
  $("detailDecisions").innerHTML = snapshot.pendingQuestions.length ? snapshot.pendingQuestions.map(question => decisionHTML(run.id, question)).join("") : `<div class="empty"><b>No pending decisions</b>This run is not waiting on you.</div>`;
}

function connectEvents(runId) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  state.stream.onmessage = async () => { if (state.selected === runId) { state.snapshot = await api(`/api/runs/${encodeURIComponent(runId)}`); renderDetail(); } refreshFleet(); };
  state.stream.addEventListener("heartbeat", () => refreshFleet());
  state.stream.onerror = () => { state.stream.close(); setTimeout(() => state.selected === runId && connectEvents(runId), 1500); };
}

function renderConfig() {
  const config = state.config, active = config.modelProfile, profiles = config.modelProfiles || [active];
  $("modelStack").textContent = active.model;
  $("profileSelect").innerHTML = profiles.map(profile => `<option value="${esc(profile.name)}" ${profile.name === config.defaultModelProfile ? "selected" : ""}>${esc(profile.name)} · ${esc(profile.model)} · ${esc(profile.apiFormat || "openai")}</option>`).join("");
  $("profile").innerHTML = profiles.map(profile => `<div class="profile-block"><div class="kv"><span>profile</span><span>${esc(profile.name)}</span></div><div class="kv"><span>provider</span><span>${esc(profile.provider)}</span></div><div class="kv"><span>protocol</span><span>${esc(profile.apiFormat || "openai")}</span></div><div class="kv"><span>model</span><span class="model-name">${esc(profile.model)}</span></div><div class="kv"><span>endpoint</span><span>${esc(profile.baseUrl)}</span></div><div class="kv"><span>secret</span><span>${esc(profile.apiKeySource)}</span></div></div>`).join("");
  $("settings").innerHTML = `<div class="kv"><span>storage root</span><span>${esc(config.storageRoot)}</span></div><div class="kv"><span>API boundary</span><span>loopback only</span></div><div class="kv"><span>event transport</span><span>SSE · resumable ID</span></div><div class="kv"><span>model profiles</span><span>${profiles.length}</span></div>`;
  const tools = config.tools || [];
  const toolRow = tool => `<label class="tool"><input type="checkbox" value="${esc(tool.id)}"><span class="tool-name">${esc(tool.id)}</span><span class="tier ${tool.requires_permission ? "gated" : ""}">${tool.requires_permission ? "ask" : "auto"} · t${tool.safety_tier}</span></label>`;
  $("toolPicker").innerHTML = tools.map(toolRow).join("") || `<div class="hint">No Rekit tools discovered.</div>`;
  $("toolCatalog").innerHTML = tools.map(tool => `<div class="tool"><span class="tool-name">${esc(tool.id)}</span><span class="tier ${tool.requires_permission ? "gated" : ""}">${tool.requires_permission ? "permission" : "automatic"} · tier ${tool.safety_tier}</span></div>`).join("");
}

$("runForm").onsubmit = async event => {
  event.preventDefault(); const button = $("launchBtn"); button.disabled = true; button.textContent = "Launching…";
  try {
    const modelTools = [...$("toolPicker").querySelectorAll("input:checked")].map(input => input.value);
    const workerRoles = $("roles").value.split(",").map(role => role.trim()).filter(Boolean);
    const result = await api("/api/runs", {method: "POST", body: JSON.stringify({target: $("target").value, goal: $("goal").value, tools: [], modelTools, workerRoles, modelProfile: $("profileSelect").value, concurrency: 4})});
    toast("Investigation launched"); await refreshFleet(); openRun(result.run.id);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Launch Investigation"; }
};

async function boot() {
  try { state.config = await api("/api/config"); renderConfig(); await refreshFleet(); setInterval(refreshFleet, 2500); }
  catch (error) { toast(error.message, true); }
}

boot();
