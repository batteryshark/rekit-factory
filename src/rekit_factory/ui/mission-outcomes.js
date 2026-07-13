"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.MissionOutcomes = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const DOMAIN = "factory-outcomes/semantic-sha256/v1";
  const FACETS = ["execution", "completion", "disposition", "validation", "acceptance", "publication"];
  const SHA256 = /^[0-9a-f]{64}$/;

  function canonicalValue(value, path = "$") {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new TypeError(`${path} contains a non-finite number`);
      return value;
    }
    if (Array.isArray(value)) return value.map((item, index) => canonicalValue(item, `${path}[${index}]`));
    if (typeof value === "object") {
      const result = {};
      Object.keys(value).sort().forEach(key => { result[key] = canonicalValue(value[key], `${path}.${key}`); });
      return result;
    }
    throw new TypeError(`${path} contains a non-JSON value`);
  }

  function canonicalSemanticText(projection) {
    if (!projection || typeof projection !== "object" || Array.isArray(projection)) {
      throw new TypeError("outcome projection must be an object");
    }
    const semantic = {};
    Object.keys(projection).sort().forEach(key => {
      if (key !== "semanticSha256" && key !== "sourceWatermarks") {
        semantic[key] = canonicalValue(projection[key], `$.${key}`);
      }
    });
    return JSON.stringify({domain: DOMAIN, projection: semantic});
  }

  async function semanticSha256(projection, subtle = globalThis.crypto?.subtle) {
    if (!subtle?.digest) throw new Error("Web Crypto SHA-256 is unavailable");
    const bytes = new TextEncoder().encode(canonicalSemanticText(projection));
    const digest = await subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
  }

  function createSemanticTracker() {
    let generation = 0;
    let acceptedSemantic = null;
    return {
      async accept(projection, digest = semanticSha256) {
        const ticket = ++generation;
        if (!projection || typeof projection !== "object" || Array.isArray(projection)) {
          acceptedSemantic = null;
          return {action: "render", integrity: "missing", projection: null};
        }
        const claimed = projection.semanticSha256;
        if (typeof claimed !== "string" || !SHA256.test(claimed)) {
          if (ticket !== generation) return {action: "stale", integrity: "legacy", projection};
          acceptedSemantic = null;
          return {action: "render", integrity: "legacy", projection};
        }
        let actual;
        try { actual = await digest(projection); }
        catch (_error) {
          if (ticket !== generation) return {action: "stale", integrity: "unavailable", projection};
          acceptedSemantic = null;
          return {action: "render", integrity: "unavailable", projection};
        }
        if (ticket !== generation) return {action: "stale", integrity: "verified", projection};
        if (actual !== claimed) {
          acceptedSemantic = null;
          return {action: "render", integrity: "mismatch", projection};
        }
        if (acceptedSemantic === claimed) return {action: "retain", integrity: "verified", projection};
        acceptedSemantic = claimed;
        return {action: "render", integrity: "verified", projection};
      },
      reset() { generation += 1; acceptedSemantic = null; },
      semantic() { return acceptedSemantic; },
    };
  }

  const primitive = value => value === null || ["string", "number", "boolean"].includes(typeof value);

  function entityText(entity) {
    const parts = [entity.entityType, entity.entityId, entity.parent?.entityType, entity.parent?.entityId];
    FACETS.forEach(name => {
      const facet = entity.facets?.[name];
      if (!facet) return;
      parts.push(name, facet.state, facet.owner);
      if (primitive(facet.rawState)) parts.push(facet.rawState);
    });
    return parts.filter(value => value !== undefined && value !== null).join(" ").toLowerCase();
  }

  function projectionView(projection, filters = {}) {
    const entities = Array.isArray(projection?.entities) ? projection.entities : [];
    const type = filters.type || "all", state = filters.state || "all";
    const owner = filters.owner || "all", terminal = filters.terminal || "all";
    const query = String(filters.query || "").trim().toLowerCase();
    const types = {}, states = new Set(), owners = new Set();
    let terminalCount = 0, unknownCount = 0, degradedCount = 0;
    entities.forEach(entity => {
      types[entity.entityType] = (types[entity.entityType] || 0) + 1;
      const facets = FACETS.map(name => entity.facets?.[name]).filter(Boolean);
      facets.forEach(facet => { states.add(facet.state); owners.add(facet.owner); });
      if (facets.some(facet => facet.terminal && facet.state !== "not-applicable")) terminalCount += 1;
      if (facets.some(facet => !facet.known || facet.state === "unknown")) unknownCount += 1;
      if ((entity.diagnostics || []).length || facets.some(facet => !facet.known)) degradedCount += 1;
    });
    const shown = entities.filter(entity => {
      const facets = FACETS.map(name => entity.facets?.[name]).filter(Boolean);
      return (type === "all" || entity.entityType === type)
        && (state === "all" || facets.some(facet => facet.state === state))
        && (owner === "all" || facets.some(facet => facet.owner === owner))
        && (terminal === "all" || facets.some(facet => facet.terminal && facet.state !== "not-applicable") === (terminal === "terminal"))
        && (!query || entityText(entity).includes(query));
    });
    return {
      entities: shown,
      options: {types: Object.keys(types).sort(), states: [...states].sort(), owners: [...owners].sort()},
      counts: {
        total: entities.length, shown: shown.length, terminal: terminalCount,
        unknown: unknownCount, degraded: degradedCount, types,
      },
    };
  }

  function canonicalLink(entity) {
    if (entity.entityType === "proof-bundle") return {tab: "dossiers", label: "Open dossiers"};
    if (entity.entityType === "operator-decision") return {tab: "decisions", label: "Open decisions"};
    if (["run", "worker", "work-item"].includes(entity.entityType)) return {tab: "activity", label: "Open activity"};
    if (entity.entityType === "finding" && entity.facets?.publication?.state === "published") {
      return {tab: "dossiers", label: "Open dossiers"};
    }
    return null;
  }

  return {DOMAIN, FACETS, canonicalLink, canonicalSemanticText, createSemanticTracker, projectionView, semanticSha256};
});
