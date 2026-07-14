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

  return {exactRoute, stableId};
});
