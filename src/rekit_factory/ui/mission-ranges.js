"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.MissionRanges = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const safe = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[character]);
  const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const list = value => Array.isArray(value) ? value : [];
  const stable = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
  const digest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const statuses = new Set(["requested", "provisioning", "ready", "in-use", "resetting", "destroyed", "expired", "failed"]);
  const actions = new Set(["observe-console", "capture-screen"]);
  const dispositions = new Set(["allowed", "denied"]);
  const short = value => String(value || "").slice(0, 12) || "—";

  function normalize(value) {
    const record = object(value);
    if (record.schemaVersion !== 1 || !stable(record.rangeId) || !statuses.has(record.status)
        || !Number.isInteger(record.revision) || record.revision < 1
        || !Number.isInteger(record.generation) || record.generation < 1
        || !digest(record.specSha256) || !digest(record.scopeSha256) || !digest(record.templateSha256)) return null;
    const nodes = list(record.nodes).slice(0, 64).flatMap(item => {
      const node = object(item);
      if (!stable(node.nodeId) || !["linux", "windows"].includes(node.platform)
          || !["x86_64", "arm64"].includes(node.architecture) || !digest(node.imageSha256)
          || !(node.environmentSha256 === null || digest(node.environmentSha256))
          || !Number.isInteger(node.toolCount) || node.toolCount < 0 || node.toolCount > 128) return [];
      return [{nodeId: node.nodeId, platform: node.platform, architecture: node.architecture,
        imageSha256: node.imageSha256, environmentSha256: node.environmentSha256, toolCount: node.toolCount}];
    });
    const attachments = list(record.attachments).slice(0, 32).flatMap(item => {
      const audit = object(item);
      if (!stable(audit.auditId) || !stable(audit.nodeId) || !actions.has(audit.action)
          || !stable(audit.requestedBy) || !dispositions.has(audit.disposition)
          || !stable(audit.reasonCode) || typeof audit.createdAt !== "string") return [];
      return [{auditId: audit.auditId, nodeId: audit.nodeId, action: audit.action,
        requestedBy: audit.requestedBy, disposition: audit.disposition,
        reasonCode: audit.reasonCode, createdAt: audit.createdAt,
        expiresAt: typeof audit.expiresAt === "string" ? audit.expiresAt : null}];
    });
    return {rangeId: record.rangeId, status: record.status, revision: record.revision,
      generation: record.generation, updatedAt: String(record.updatedAt || ""), expiresAt: String(record.expiresAt || ""),
      specSha256: record.specSha256, scopeSha256: record.scopeSha256, templateSha256: record.templateSha256,
      topologySha256: digest(record.topologySha256) ? record.topologySha256 : null,
      executionSha256: digest(record.executionSha256) ? record.executionSha256 : null,
      benchmarkComparisonKey: typeof record.benchmarkComparisonKey === "string" && /^sha256:[0-9a-f]{64}$/.test(record.benchmarkComparisonKey) ? record.benchmarkComparisonKey : null,
      nodes, attachments, failure: object(record.failure)};
  }

  function renderBoard(values) {
    const records = list(values).slice(0, 64).map(normalize).filter(Boolean);
    if (!records.length) return `<div class="empty"><b>No qualified range leases</b>Real adapter health will appear only after a provider-neutral projection is configured.</div>`;
    return records.map(record => `<article class="range-card range-${safe(record.status)}" data-range-id="${safe(record.rangeId)}">
      <header><div><span>RANGE · GENERATION ${record.generation}</span><b>${safe(record.rangeId)}</b></div><em>${safe(record.status.replaceAll("-", " "))}</em></header>
      <div class="range-health-grid"><span><b>${record.nodes.length}</b>nodes</span><span><b>${record.attachments.length}</b>attach audits</span><span><b>${record.revision}</b>revision</span><span><b>${safe(short(record.topologySha256))}</b>topology</span></div>
      <footer><code>${safe(short(record.specSha256))}</code><span>${record.executionSha256 ? "runtime identity bound" : "runtime identity unavailable"}</span></footer>
    </article>`).join("");
  }

  function renderDetail(value) {
    const record = normalize(value);
    if (!record) return `<div class="empty"><b>Range projection unavailable</b>The record failed bounded client validation.</div>`;
    const nodes = record.nodes.map(node => `<div class="range-node"><div><b>${safe(node.nodeId)}</b><span>${safe(node.platform)} · ${safe(node.architecture)}</span></div><code>image ${safe(short(node.imageSha256))}</code><small>${node.environmentSha256 ? `environment ${safe(short(node.environmentSha256))} · ${node.toolCount} tools` : "runtime identity not published"}</small></div>`).join("");
    const audits = record.attachments.length ? record.attachments.map(audit => `<div class="range-attachment ${safe(audit.disposition)}"><span>${safe(audit.action.replaceAll("-", " "))}</span><b>${safe(audit.disposition)}</b><code>${safe(audit.nodeId)} · ${safe(audit.reasonCode)}</code><small>${safe(audit.requestedBy)} · ${safe(audit.createdAt)}</small></div>`).join("") : `<div class="empty compact"><b>No attachment audits</b>Headless operation requires no observer session.</div>`;
    return `<div class="range-detail"><div class="range-detail-head"><div><span>PROVIDER-NEUTRAL LEASE</span><h3>${safe(record.rangeId)}</h3></div><b>${safe(record.status)}</b></div><div class="range-identity"><code>spec ${safe(record.specSha256)}</code><code>scope ${safe(record.scopeSha256)}</code><code>template ${safe(record.templateSha256)}</code>${record.benchmarkComparisonKey ? `<code>comparison ${safe(record.benchmarkComparisonKey)}</code>` : ""}</div><section><h4>Nodes</h4>${nodes}</section><section><h4>Read-only attachment audit</h4>${audits}</section></div>`;
  }

  return {normalize, renderBoard, renderDetail};
});
