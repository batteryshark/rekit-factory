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
      if (key !== "semanticSha256" && key !== "semanticCanonicalBase64" && key !== "sourceWatermarks") {
        semantic[key] = canonicalValue(projection[key], `$.${key}`);
      }
    });
    return JSON.stringify({domain: DOMAIN, projection: semantic});
  }

  function decodeBase64(value) {
    if (typeof value !== "string" || !value.length || value.length % 4 !== 0
        || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
      throw new TypeError("semanticCanonicalBase64 must be standard Base64");
    }
    let binary;
    if (typeof globalThis.atob === "function") binary = globalThis.atob(value);
    else if (typeof Buffer === "function") binary = Buffer.from(value, "base64").toString("binary");
    else throw new Error("Base64 decoding is unavailable");
    const canonical = typeof globalThis.btoa === "function"
      ? globalThis.btoa(binary) : Buffer.from(binary, "binary").toString("base64");
    if (canonical !== value) throw new TypeError("semanticCanonicalBase64 is not canonical");
    return Uint8Array.from(binary, character => character.charCodeAt(0));
  }

  function decodeSemanticEnvelope(source) {
    const encoded = source?.semanticCanonicalBase64;
    const bytes = decodeBase64(encoded);
    let envelope;
    try { envelope = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes)); }
    catch (_error) { throw new TypeError("semantic canonical envelope must be valid UTF-8 JSON"); }
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)
        || Object.keys(envelope).sort().join(",") !== "domain,projection"
        || envelope.domain !== DOMAIN || !envelope.projection
        || typeof envelope.projection !== "object" || Array.isArray(envelope.projection)) {
      throw new TypeError("semantic canonical envelope has an invalid domain or shape");
    }
    if (["semanticSha256", "semanticCanonicalBase64", "sourceWatermarks"]
        .some(key => Object.hasOwn(envelope.projection, key))) {
      throw new TypeError("semantic canonical envelope contains nonsemantic fields");
    }
    return {
      bytes,
      fingerprint: `canonical:${encoded}`,
      projection: {
        ...envelope.projection,
        semanticSha256: source.semanticSha256,
        semanticCanonicalBase64: encoded,
        sourceWatermarks: source.sourceWatermarks || {},
      },
    };
  }

  async function sha256Bytes(bytes, subtle = globalThis.crypto?.subtle) {
    if (!subtle?.digest) throw new Error("Web Crypto SHA-256 is unavailable");
    const digest = await subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
  }

  async function semanticSha256(projection, subtle = globalThis.crypto?.subtle) {
    return sha256Bytes(decodeSemanticEnvelope(projection).bytes, subtle);
  }

  function createSemanticTracker() {
    let generation = 0;
    let acceptedFingerprint = null;
    let acceptedIntegrity = null;
    const settle = (ticket, integrity, projection, fingerprint) => {
      if (ticket !== generation) return {action: "stale", integrity, projection};
      const action = acceptedFingerprint === fingerprint && acceptedIntegrity === integrity
        ? "retain" : "render";
      acceptedFingerprint = fingerprint;
      acceptedIntegrity = integrity;
      return {action, integrity, projection};
    };
    return {
      async accept(source, digest = sha256Bytes) {
        const ticket = ++generation;
        if (!source || typeof source !== "object" || Array.isArray(source)) {
          return settle(ticket, "missing", null, "missing");
        }
        if (!Object.hasOwn(source, "semanticCanonicalBase64")) {
          let fingerprint;
          try { fingerprint = `legacy:${canonicalSemanticText(source)}`; }
          catch (_error) { return settle(ticket, "invalid-envelope", null, "invalid:legacy"); }
          return settle(ticket, "legacy", source, fingerprint);
        }
        if (typeof source.semanticCanonicalBase64 !== "string") {
          return settle(ticket, "invalid-envelope", null, "invalid:non-string");
        }
        let prepared;
        try { prepared = decodeSemanticEnvelope(source); }
        catch (_error) {
          return settle(
            ticket, "invalid-envelope", null,
            `invalid:${String(source.semanticCanonicalBase64)}`,
          );
        }
        const claimed = source.semanticSha256;
        if (typeof claimed !== "string" || !SHA256.test(claimed)) {
          return settle(ticket, "mismatch", prepared.projection, prepared.fingerprint);
        }
        let actual;
        try { actual = await digest(prepared.bytes); }
        catch (_error) {
          return settle(ticket, "unavailable", prepared.projection, prepared.fingerprint);
        }
        if (actual !== claimed) {
          return settle(ticket, "mismatch", prepared.projection, prepared.fingerprint);
        }
        return settle(ticket, "verified", prepared.projection, prepared.fingerprint);
      },
      reset() { generation += 1; acceptedFingerprint = null; acceptedIntegrity = null; },
      fingerprint() { return acceptedFingerprint; },
    };
  }

  function createGenerationGate() {
    let generation = 0;
    return {
      begin() { generation += 1; return generation; },
      invalidate() { generation += 1; },
      isCurrent(ticket) { return ticket === generation; },
    };
  }

  function isCurrentEventStream(stream, activeStream, runId, selectedRunId) {
    return stream === activeStream && runId === selectedRunId;
  }

  function latestEventId(events) {
    if (!Array.isArray(events) || !events.length) return null;
    const id = events[events.length - 1]?.id;
    return id === undefined || id === null || id === "" ? null : String(id);
  }

  function eventStreamUrl(runId, cursor = null) {
    const base = `/api/runs/${encodeURIComponent(runId)}/events`;
    return cursor ? `${base}?after=${encodeURIComponent(cursor)}` : base;
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

  return {DOMAIN, FACETS, canonicalLink, canonicalSemanticText, createGenerationGate, createSemanticTracker, decodeSemanticEnvelope, eventStreamUrl, isCurrentEventStream, latestEventId, projectionView, semanticSha256, sha256Bytes};
});
