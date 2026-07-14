"use strict";

(function (root, factory) {
  const value = factory();
  if (typeof module === "object" && module.exports) module.exports = value;
  else root.MissionNotifications = value;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const stableId = value => typeof value === "string" && value.length > 0 && value.length <= 128
    && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value);

  function exactRoute(link) {
    if (!link || typeof link !== "object" || Array.isArray(link)) return null;
    const keys = Object.keys(link).sort().join(",");
    if (link.view !== "mission-control" || !stableId(link.entityId)) return null;
    if (keys === "entityId,entityType,tab,view" && link.entityType === "campaign"
        && link.tab === "campaigns") {
      return {entityType: "campaign", entityId: link.entityId, surface: "campaigns"};
    }
    if (keys !== "entityId,entityType,runId,tab,view" || !stableId(link.runId)) return null;
    const routes = {
      "operator-decision": {wireTab: "decisions", surface: "decisions"},
      finding: {wireTab: "findings", surface: "outcomes"},
      "proof-bundle": {wireTab: "dossiers", surface: "dossiers"},
    };
    const route = routes[link.entityType];
    if (!route || link.tab !== route.wireTab) return null;
    return {runId: link.runId, entityType: link.entityType,
      entityId: link.entityId, surface: route.surface};
  }

  function routeContract(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)
        || Object.keys(value).sort().join(",") !== "maxLength,queryMarker,routes,schemaVersion"
        || value.schemaVersion !== 1 || value.queryMarker !== "mc-v1"
        || value.maxLength !== 512 || !Array.isArray(value.routes)
        || value.routes.length !== 4) return null;
    const expected = [
      {entityType: "campaign", surface: "campaigns", requiresRun: false},
      {entityType: "finding", surface: "outcomes", requiresRun: true},
      {entityType: "operator-decision", surface: "decisions", requiresRun: true},
      {entityType: "proof-bundle", surface: "dossiers", requiresRun: true},
    ];
    for (let index = 0; index < expected.length; index += 1) {
      const route = value.routes[index];
      if (!route || typeof route !== "object" || Array.isArray(route)
          || Object.keys(route).sort().join(",") !== "entityType,requiresRun,surface"
          || route.entityType !== expected[index].entityType
          || route.surface !== expected[index].surface
          || route.requiresRun !== expected[index].requiresRun) return null;
    }
    return {schemaVersion: 1, queryMarker: "mc-v1", maxLength: 512, routes: expected};
  }

  function parseUrlRoute(search, declared) {
    const contract = routeContract(declared);
    if (!contract || typeof search !== "string" || search.length > contract.maxLength
        || (search && !search.startsWith("?"))) return null;
    const parameters = new URLSearchParams(search);
    const entries = [...parameters.entries()];
    if (entries.some(([key, value], index) => !stableId(key) || !stableId(value)
        || entries.findIndex(item => item[0] === key) !== index)) return null;
    const object = Object.fromEntries(entries);
    if (object.mc !== contract.queryMarker) return null;
    const record = contract.routes.find(item => item.surface === object.tab
      && item.entityType === object.type);
    if (!record || !stableId(object.entity)) return null;
    const expectedKeys = record.requiresRun ? ["entity", "mc", "run", "tab", "type"]
      : ["entity", "mc", "tab", "type"];
    if (Object.keys(object).sort().join(",") !== expectedKeys.join(",")
        || (record.requiresRun && !stableId(object.run))) return null;
    return {entityType: record.entityType, entityId: object.entity,
      surface: record.surface, ...(record.requiresRun ? {runId: object.run} : {})};
  }

  function urlSearch(route, declared) {
    const contract = routeContract(declared);
    if (!contract || !route || typeof route !== "object" || Array.isArray(route)) return null;
    const record = contract.routes.find(item => item.surface === route.surface
      && item.entityType === route.entityType);
    if (!record || !stableId(route.entityId)
        || (record.requiresRun && !stableId(route.runId))) return null;
    const parameters = new URLSearchParams({mc: contract.queryMarker,
      tab: record.surface, type: record.entityType, entity: route.entityId});
    if (record.requiresRun) parameters.set("run", route.runId);
    const result = `?${parameters.toString()}`;
    return result.length <= contract.maxLength ? result : null;
  }

  function canonicalTarget(route, snapshot, campaigns) {
    if (!route || typeof route !== "object" || Array.isArray(route)) return false;
    if (route.surface === "campaigns" && route.entityType === "campaign"
        && Object.keys(route).sort().join(",") === "entityId,entityType,surface") {
      return Array.isArray(campaigns) && campaigns.some(item => item?.campaignId === route.entityId
        && item.health?.degraded !== true);
    }
    const exactKeys = "entityId,entityType,runId,surface";
    const expected = {finding: "outcomes", "operator-decision": "decisions",
      "proof-bundle": "dossiers"};
    if (Object.keys(route).sort().join(",") !== exactKeys
        || expected[route.entityType] !== route.surface || !stableId(route.runId)
        || !stableId(route.entityId) || snapshot?.run?.id !== route.runId
        || snapshot?.outcomeProjection?.degraded !== false
        || !Array.isArray(snapshot.outcomeProjection.entities)
        || !snapshot.outcomeProjection.entities.some(item => item?.entityType === route.entityType
          && item?.entityId === route.entityId)) return false;
    if (route.surface === "decisions") return Array.isArray(snapshot.pendingQuestions)
      && snapshot.pendingQuestions.some(item => item?.id === route.entityId);
    if (route.surface === "dossiers") return Array.isArray(snapshot.dossiers)
      && snapshot.dossiers.some(item => item?.id === route.entityId);
    return true;
  }

  function focusPlan(route, snapshot, campaigns) {
    if (!canonicalTarget(route, snapshot, campaigns)) return null;
    const plan = {surface: route.surface, entityType: route.entityType,
      entityId: route.entityId};
    if (route.runId) plan.runId = route.runId;
    if (route.surface === "outcomes") plan.outcomeFilters = {
      query: route.entityId, exactId: route.entityId, type: route.entityType,
      state: "all", owner: "all", terminal: "all",
    };
    return plan;
  }

  function restorationPlan(search, declared, snapshot, campaigns) {
    const route = parseUrlRoute(search, declared);
    return route ? focusPlan(route, snapshot, campaigns) : null;
  }

  return {canonicalTarget, exactRoute, focusPlan, parseUrlRoute, restorationPlan,
    routeContract, stableId, urlSearch};
});
